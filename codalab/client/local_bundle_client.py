'''
LocalBundleClient is BundleClient implementation that interacts directly with a
BundleStore and a BundleModel. All filesystem operations are handled locally.
'''
from time import sleep
import contextlib
import os

from codalab.bundles import (
  get_bundle_subclass,
  UPLOADED_TYPES,
)
from codalab.common import (
  precondition,
  State,
  UsageError,
    AuthorizationError,
)
from codalab.client.bundle_client import BundleClient
from codalab.lib import (
  canonicalize,
  path_util,
  file_util,
  worksheet_util,
)
from codalab.objects.worksheet import Worksheet
from codalab.objects import permission
from codalab.objects.permission import (
    check_has_full_permission,
    check_has_read_permission,
    Group,
    parse_permission
)

def authentication_required(func):
    def decorate(self, *args, **kwargs):
        if self.auth_handler.current_user() is None:
            raise AuthorizationError("Not authenticated")
        return func(self, *args, **kwargs)
    return decorate

class LocalBundleClient(BundleClient):
    def __init__(self, address, bundle_store, model, auth_handler, verbose):
        self.address = address
        self.bundle_store = bundle_store
        self.model = model
        self.auth_handler = auth_handler
        self.verbose = verbose

    def _current_user_id(self):
        return self.auth_handler.current_user().unique_id

    def _bundle_to_bundle_info(self, bundle, children=None):
        '''
        Helper: Convert bundle to bundle_info.
        '''
        hard_dependencies = bundle.get_hard_dependencies()
        # See tables.py
        result = {
          'uuid': bundle.uuid,
          'bundle_type': bundle.bundle_type,
          'command': bundle.command,
          'data_hash': bundle.data_hash,
          'state': bundle.state,
          'metadata': bundle.metadata.to_dict(),
          'dependencies': [dep.to_dict() for dep in bundle.dependencies],
          'hard_dependencies': [dep.to_dict() for dep in hard_dependencies]
        }
        if children is not None:
            result['children'] = [child.simple_str() for child in children]
        return result

    def get_bundle_uuid(self, worksheet_uuid, bundle_spec):
        return canonicalize.get_bundle_uuid(self.model, worksheet_uuid, bundle_spec)

    def get_bundle_uuids(self, worksheet_uuid, bundle_spec):
        return canonicalize.get_bundle_uuids(self.model, worksheet_uuid, bundle_spec)

    # Helper
    def get_target_path(self, target):
        return canonicalize.get_target_path(self.bundle_store, self.model, target)

    # Helper
    def get_bundle_target(self, target):
        (bundle_uuid, subpath) = target
        return (self.model.get_bundle(bundle_uuid), subpath)

    def get_worksheet_uuid(self, worksheet_spec):
        # Create default worksheet if necessary
        if worksheet_spec == Worksheet.DEFAULT_WORKSHEET_NAME:
            try:
                return canonicalize.get_worksheet_uuid(self.model, worksheet_spec)
            except UsageError:
                return self.new_worksheet(worksheet_spec)
        else:
            return canonicalize.get_worksheet_uuid(self.model, worksheet_spec)

    def expand_worksheet_item(self, worksheet_uuid, item):
        (bundle_spec, value, type) = item
        if bundle_spec is None:
            return (None, value or '', type or '')
        try:
            bundle_uuid = self.get_bundle_uuid(worksheet_uuid, bundle_spec)
        except UsageError, e:
            return (bundle_spec, str(e) if value is None else value, '')
        if bundle_uuid != bundle_spec and value is None:
            # The user specified a bundle for the first time without help text.
            # Produce some auto-generated help text here.
            bundle = self.model.get_bundle(bundle_uuid)
            value = bundle_spec
            if getattr(bundle.metadata, 'description', None):
                value = bundle.metadata.name
        return (bundle_uuid, value or '', type or '')

    def validate_user_metadata(self, bundle_subclass, metadata):
        '''
        Check that the user did not supply values for any auto-generated metadata.
        Raise a UsageError with the offending keys if they are.
        '''
        legal_keys = set(spec.key for spec in bundle_subclass.get_user_defined_metadata())
        illegal_keys = [key for key in metadata if key not in legal_keys]
        if illegal_keys:
            raise UsageError('Illegal metadata keys: %s' % (', '.join(illegal_keys),))

    def upload_bundle(self, bundle_type, path, construct_args, worksheet_uuid):
        existing = 'uuid' in construct_args
        metadata = construct_args['metadata']
        message = 'Invalid upload bundle_type: %s' % (bundle_type,)
        if not existing:
            precondition(bundle_type in UPLOADED_TYPES, message)
        bundle_subclass = get_bundle_subclass(bundle_type)
        if not existing:
            self.validate_user_metadata(bundle_subclass, metadata)

        # Upload the given path and record additional metadata from the upload.
        (data_hash, bundle_store_metadata) = self.bundle_store.upload(path)
        metadata.update(bundle_store_metadata)
        # TODO: check that if the data hash already exists, it's the same as before.
        construct_args['data_hash'] = data_hash

        bundle = bundle_subclass.construct(**construct_args)
        self.model.save_bundle(bundle)
        if worksheet_uuid:
            self.add_worksheet_item(worksheet_uuid, bundle.uuid)
        return bundle.uuid

    def derive_bundle(self, bundle_type, targets, command, metadata, worksheet_uuid):
        '''
        For both make and run bundles.
        '''
        bundle_subclass = get_bundle_subclass(bundle_type)
        self.validate_user_metadata(bundle_subclass, metadata)
        bundle = bundle_subclass.construct(targets=targets, command=command, metadata=metadata)
        self.model.save_bundle(bundle)
        if worksheet_uuid:
            self.add_worksheet_item(worksheet_uuid, bundle.uuid)
        return bundle.uuid

    def update_bundle_metadata(self, uuid, metadata):
        bundle = self.model.get_bundle(uuid)
        self.validate_user_metadata(bundle, metadata)
        self.model.update_bundle(bundle, {'metadata': metadata})

    def delete_bundle(self, uuid, force=False):
        children = self.model.get_children(uuid)
        if children and not force:
            raise UsageError('The following bundles depend on %s:\n  %s' % (
              uuid,
              '\n  '.join(child.simple_str() for child in children),
            ))
        child_worksheets = self.model.get_child_worksheets(uuid)
        if child_worksheets and not force:
            raise UsageError('The following worksheets depend on %s:\n  %s' % (
              uuid,
              '\n  '.join(child.simple_str() for child in child_worksheets),
            ))
        self.model.delete_bundle_tree([uuid], force=force)

    def get_bundle_info(self, uuid, get_children=False):
        '''
        Return information about the bundle.
        get_children: whether we want to return information about the children too.
        '''
        bundle = self.model.get_bundle(uuid)
        children = self.model.get_children(uuid) if get_children else None
        return self._bundle_to_bundle_info(bundle, children=children)

    # Return information about an individual target inside the bundle.

    def get_target_info(self, target, depth):
        path = self.get_target_path(target)
        return path_util.get_info(path, depth)

    def cat_target(self, target, out):
        path = self.get_target_path(target)
        path_util.cat(path, out)

    def head_target(self, target, num_lines):
        path = self.get_target_path(target)
        return path_util.read_lines(path, num_lines)

    def open_target_handle(self, target):
        path = self.get_target_path(target)
        return open(path) if path and os.path.exists(path) else None
    def close_target_handle(self, handle):
        handle.close()

    def download_target(self, target):
        # Don't need to download anything because it's already local.
        return (self.get_target_path(target), None)

    #############################################################################
    # Implementations of worksheet-related client methods follow!
    #############################################################################

    @authentication_required
    def new_worksheet(self, name):
        worksheet = Worksheet({'name': name, 'items': [], 'owner_id': self._current_user_id()})
        self.model.save_worksheet(worksheet)
        return worksheet.uuid

    def list_worksheets(self):
        current_user = self.auth_handler.current_user()
        if current_user is None:
            return self.model.list_worksheets()
        else:
            return self.model.list_worksheets(current_user.unique_id)

    def get_worksheet_info(self, worksheet_spec):
        uuid = self.get_worksheet_uuid(worksheet_spec)
        worksheet = self.model.get_worksheet(uuid)
        current_user = self.auth_handler.current_user()
        current_user_id = None if current_user is None else current_user.unique_id
        check_has_read_permission(self.model, current_user_id, worksheet)
        result = worksheet.get_info_dict()
        # We need to do some finicky stuff here to convert the bundle_uuids into
        # bundle info dicts. However, we still make O(1) database calls because we
        # use the optimized batch_get_bundles multiget method.
        uuids = set(
            bundle_uuid for (bundle_uuid, _, _) in result['items']
          if bundle_uuid is not None
        )
        bundles = self.model.batch_get_bundles(uuid=uuids)
        bundle_dict = {bundle.uuid: self._bundle_to_bundle_info(bundle) for bundle in bundles}

        # If a bundle uuid is orphaned, we still have to return the uuid in a dict.
        items = []
        result['items'] = [
          (
               None if bundle_uuid is None else
               bundle_dict.get(bundle_uuid, {'uuid': bundle_uuid}),
                    worksheet_util.expand_worksheet_item_info(value, type),
                    type,
          )
            for (bundle_uuid, value, type) in result['items']
        ]
        return result

    @authentication_required
    def add_worksheet_item(self, worksheet_spec, bundle_spec):
        worksheet_uuid = self.get_worksheet_uuid(worksheet_spec)
        worksheet = self.model.get_worksheet(worksheet_uuid)
        check_has_full_permission(self.model, self._current_user_id(), worksheet)
        bundle_uuid = self.get_bundle_uuid(worksheet_uuid, bundle_spec)
        bundle = self.model.get_bundle(bundle_uuid)
        # Compute a nice value for this item, using the description if it exists.
        item_value = bundle_spec
        if getattr(bundle.metadata, 'description', None):
            item_value = bundle.metadata.name
        item = (bundle.uuid, item_value, 'bundle')
        self.model.add_worksheet_item(worksheet_uuid, item)

    @authentication_required
    def update_worksheet(self, worksheet_info, new_items):
        # Convert (bundle_spec, value) pairs into canonical (bundle_uuid, value, type) pairs.
        # This step could take O(n) database calls! However, it will only hit the
        # database for each bundle the user has newly specified by name - bundles
        # that were already in the worksheet will be referred to by uuid, so
        # get_bundle_uuid will be an in-memory call for these. This hit is acceptable.
        worksheet_uuid = worksheet_info['uuid']
        canonical_items = [self.expand_worksheet_item(worksheet_uuid, item) for item in new_items]
        last_item_id = worksheet_info['last_item_id']
        length = len(worksheet_info['items'])
        worksheet = self.model.get_worksheet(worksheet_uuid)
        check_has_full_permission(self.model, self._current_user_id(), worksheet)
        try:
            self.model.update_worksheet(
              worksheet_uuid, last_item_id, length, canonical_items)
        except UsageError:
            # Turn the model error into a more readable one using the object.
            raise UsageError('%s was updated concurrently!' % (worksheet,))

    @authentication_required
    def rename_worksheet(self, worksheet_spec, name):
        uuid = self.get_worksheet_uuid(worksheet_spec)
        worksheet = self.model.get_worksheet(uuid)
        check_has_full_permission(self.model, self._current_user_id(), worksheet)
        self.model.rename_worksheet(worksheet, name)

    @authentication_required
    def delete_worksheet(self, worksheet_spec):
        uuid = self.get_worksheet_uuid(worksheet_spec)
        worksheet = self.model.get_worksheet(uuid)
        check_has_full_permission(self.model, self._current_user_id(), worksheet)
        self.model.delete_worksheet(uuid)

    #############################################################################
    # Commands related to groups and permissions follow!
    #############################################################################

    @authentication_required
    def list_groups(self):
        group_dicts = self.model.batch_get_all_groups(
            None,
            {'owner_id': self._current_user_id(), 'user_defined': True},
            {'user_id': self._current_user_id()})
        for group_dict in group_dicts:
            role = 'member'
            if group_dict['is_admin'] == True:
                if group_dict['owner_id'] == group_dict['user_id']:
                    role = 'owner'
                else:
                    role = 'co-owner'
            group_dict['role'] = role
        return group_dicts

    @authentication_required
    def new_group(self, name):
        group = Group({'name': name, 'user_defined': True, 'owner_id': self._current_user_id()})
        group.validate()
        group_dict = self.model.create_group(group.to_dict())
        return group_dict

    @authentication_required
    def rm_group(self, group_spec):
        group_info = permission.unique_group_managed_by(self.model, group_spec, self._current_user_id())
        if group_info['owner_id'] != self._current_user_id():
            raise UsageError('A group cannot be deleted by its co-owners.')
        self.model.delete_group(group_info['uuid'])
        return group_info

    @authentication_required
    def group_info(self, group_spec):
        group_info = permission.unique_group_with_user(self.model, group_spec, self._current_user_id())
        users_in_group = self.model.batch_get_user_in_group(group_uuid=group_info['uuid'])
        user_ids = [group_info['owner_id']]
        user_ids.extend([u['user_id'] for u in users_in_group])
        users = self.auth_handler.get_users('ids', user_ids)
        members = []
        roles = {}
        for row in users_in_group:
            roles[row['user_id']] = 'co-owner' if row['is_admin'] == True else 'member'
        roles[group_info['owner_id']] = 'owner'
        for user_id in user_ids:
            if user_id in users:
                user = users[user_id]
                members.append({'name': user.name, 'role': roles[user_id]})
        group_info['members'] = members
        return group_info

    @authentication_required
    def add_user(self, username, group_spec, is_admin):
        group_info = permission.unique_group_managed_by(self.model, group_spec, self._current_user_id())
        users = self.auth_handler.get_users('names', [username])
        user = users[username]
        if user is None:
            raise UsageError("%s is not a valid user." % (username,))
        if user.unique_id == self._current_user_id():
            raise UsageError("You cannot add yourself to a group.")
        members = self.model.batch_get_user_in_group(user_id=user.unique_id, group_uuid=group_info['uuid'])
        if len(members) > 0:
            member = members[0]
            if user.unique_id == group_info['owner_id']:
                raise UsageError("You cannot modify the owner a group.")
            if member['is_admin'] != is_admin:
                self.model.update_user_in_group(user.unique_id, group_info['uuid'], is_admin)
                member['operation'] = 'Modified'
        else:
            member = self.model.add_user_in_group(user.unique_id, group_info['uuid'], is_admin)
            member['operation'] = 'Added'
        member['name'] = username
        return member

    @authentication_required
    def rm_user(self, username, group_spec):
        group_info = permission.unique_group_managed_by(self.model, group_spec, self._current_user_id())
        users = self.auth_handler.get_users('names', [username])
        user = users[username]
        if user is None:
            raise UsageError("%s is not a valid user." % (username,))
        if user.unique_id == group_info['owner_id']:
            raise UsageError("You cannot modify the owner a group.")
        members = self.model.batch_get_user_in_group(user_id=user.unique_id, group_uuid=group_info['uuid'])
        if len(members) > 0:
            member = members[0]
            self.model.delete_user_in_group(user.unique_id, group_info['uuid'])
            member['name'] = username
            return member
        return None

    @authentication_required
    def set_worksheet_perm(self, worksheet_spec, permission_name, group_spec):
        uuid = self.get_worksheet_uuid(worksheet_spec)
        worksheet = self.model.get_worksheet(uuid)
        check_has_full_permission(self.model, self._current_user_id(), worksheet)
        new_permission = parse_permission(permission_name)
        group_info = permission.unique_group(self.model, group_spec)
        old_permissions = self.model.get_permission(group_info['uuid'], worksheet.uuid)
        if new_permission == 0:
            if len(old_permissions) > 0:
                self.model.delete_permission(group_info['uuid'], worksheet.uuid)
        else:
            if len(old_permissions) == 1:
                self.model.update_permission(group_info['uuid'], worksheet.uuid, new_permission)
            else:
                if len(old_permissions) > 0:
                    self.model.delete_permission(group_info['uuid'], worksheet.uuid)
                self.model.add_permission(group_info['uuid'], worksheet.uuid, new_permission)
        return {'worksheet': worksheet,
                'group_info': group_info,
                'permission': new_permission}
