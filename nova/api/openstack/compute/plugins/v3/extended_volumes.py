# vim: tabstop=4 shiftwidth=4 softtabstop=4

#   Copyright 2013 OpenStack Foundation
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.

"""The Extended Volumes API extension."""
from webob import exc

from nova.api.openstack import common
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.api.openstack import xmlutil
from nova import compute
from nova import exception
from nova.openstack.common import log as logging
from nova.openstack.common import uuidutils
from nova import volume

ALIAS = "os-extended-volumes"
LOG = logging.getLogger(__name__)
authorize = extensions.soft_extension_authorizer('compute', 'v3:' + ALIAS)
authorize_attach = extensions.soft_extension_authorizer('compute',
                                                        'v3:%s:attach' % ALIAS)
authorize_detach = extensions.soft_extension_authorizer('compute',
                                                        'v3:%s:detach' % ALIAS)


class ExtendedVolumesController(wsgi.Controller):
    def __init__(self, *args, **kwargs):
        super(ExtendedVolumesController, self).__init__(*args, **kwargs)
        self.compute_api = compute.API()
        self.volume_api = volume.API()

    def _extend_server(self, context, server, instance):
        bdms = self.compute_api.get_instance_bdms(context, instance)
        volume_ids = [bdm['volume_id'] for bdm in bdms if bdm['volume_id']]
        key = "%s:volumes_attached" % ExtendedVolumes.alias
        server[key] = [{'id': volume_id} for volume_id in volume_ids]

    @wsgi.extends
    def show(self, req, resp_obj, id):
        context = req.environ['nova.context']
        if authorize(context):
            # Attach our subordinate template to the response object
            resp_obj.attach(xml=ExtendedVolumesServerTemplate())
            server = resp_obj.obj['server']
            db_instance = req.get_db_instance(server['id'])
            # server['id'] is guaranteed to be in the cache due to
            # the core API adding it in its 'show' method.
            self._extend_server(context, server, db_instance)

    @wsgi.extends
    def detail(self, req, resp_obj):
        context = req.environ['nova.context']
        if authorize(context):
            # Attach our subordinate template to the response object
            resp_obj.attach(xml=ExtendedVolumesServersTemplate())
            servers = list(resp_obj.obj['servers'])
            for server in servers:
                db_instance = req.get_db_instance(server['id'])
                # server['id'] is guaranteed to be in the cache due to
                # the core API adding it in its 'detail' method.
                self._extend_server(context, server, db_instance)

    def _validate_volume_id(self, volume_id):
        if not uuidutils.is_uuid_like(volume_id):
            msg = _("Bad volumeId format: volumeId is "
                    "not in proper format (%s)") % volume_id
            raise exc.HTTPBadRequest(explanation=msg)

    @wsgi.response(202)
    @wsgi.action('attach')
    def attach(self, req, id, body):
        server_id = id
        context = req.environ['nova.context']
        authorize_attach(context)

        if not self.is_valid_body(body, 'attach'):
            raise exc.HTTPBadRequest(_("The request body invalid"))

        volume_id = body['attach']['volume_id']
        device = body['attach'].get('device')

        self._validate_volume_id(volume_id)

        LOG.audit(_("Attach volume %(volume_id)s to instance %(server_id)s "
                    "at %(device)s"),
                  {'volume_id': volume_id,
                   'device': device,
                   'server_id': server_id},
                  context=context)

        try:
            instance = self.compute_api.get(context, server_id)
            self.compute_api.attach_volume(context, instance,
                                           volume_id, device)
        except (exception.InstanceNotFound, exception.VolumeNotFound) as e:
            raise exc.HTTPNotFound(explanation=e.format_message())
        except exception.InstanceInvalidState as state_error:
            common.raise_http_conflict_for_instance_invalid_state(
                state_error, 'attach_volume')
        except exception.InvalidVolume as e:
            raise exc.HTTPBadRequest(explanation=e.format_message())
        except exception.InvalidDevicePath as e:
            raise exc.HTTPBadRequest(explanation=e.format_message())

    @wsgi.response(202)
    @wsgi.action('detach')
    def detach(self, req, id, body):
        server_id = id
        context = req.environ['nova.context']
        authorize_detach(context)

        volume_id = body['detach']['volume_id']
        LOG.audit(_("Detach volume %(volume_id)s from "
                    "instance %(server_id)s"),
                  {"volume_id": volume_id,
                   "server_id": id,
                   "context": context})
        try:
            instance = self.compute_api.get(context, server_id)
        except exception.InstanceNotFound as e:
            raise exc.HTTPNotFound(explanation=e.format_message())

        try:
            volume = self.volume_api.get(context, volume_id)
        except exception.VolumeNotFound as e:
            raise exc.HTTPNotFound(explanation=e.format_message())

        bdms = self.compute_api.get_instance_bdms(context, instance)
        if not bdms:
            msg = _("Volume %(volume_id)s is not attached to the "
                    "instance %(server_id)s") % {'server_id': server_id,
                                                 'volume_id': volume_id}
            LOG.debug(msg)
            raise exc.HTTPNotFound(explanation=msg)

        for bdm in bdms:
            if bdm['volume_id'] != volume_id:
                continue
            try:
                self.compute_api.detach_volume(context, instance, volume)
                break
            except exception.VolumeUnattached:
                # The volume is not attached.  Treat it as NotFound
                # by falling through.
                pass
            except exception.InvalidVolume as e:
                raise exc.HTTPBadRequest(explanation=e.format_message())
            except exception.InstanceInvalidState as state_error:
                common.raise_http_conflict_for_instance_invalid_state(
                    state_error, 'detach_volume')
        else:
            msg = _("Volume %(volume_id)s is not attached to the "
                    "instance %(server_id)s") % {'server_id': server_id,
                                                 'volume_id': volume_id}
            raise exc.HTTPNotFound(explanation=msg)


class ExtendedVolumes(extensions.V3APIExtensionBase):
    """Extended Volumes support."""

    name = "ExtendedVolumes"
    alias = ALIAS
    namespace = ("http://docs.openstack.org/compute/ext/"
                 "extended_volumes/api/v3")
    version = 1

    def get_controller_extensions(self):
        controller = ExtendedVolumesController()
        extension = extensions.ControllerExtension(self, 'servers', controller)
        return [extension]

    def get_resources(self):
        return []


def make_server(elem):
    volumes = xmlutil.SubTemplateElement(
        elem, '{%s}volume_attached' % ExtendedVolumes.namespace,
        selector='%s:volumes_attached' % ExtendedVolumes.alias)
    volumes.set('id')


class ExtendedVolumesServerTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('server', selector='server')
        make_server(root)
        return xmlutil.SubordinateTemplate(root, 1, nsmap={
            ExtendedVolumes.alias: ExtendedVolumes.namespace})


class ExtendedVolumesServersTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('servers')
        elem = xmlutil.SubTemplateElement(root, 'server', selector='servers')
        make_server(elem)
        return xmlutil.SubordinateTemplate(root, 1, nsmap={
            ExtendedVolumes.alias: ExtendedVolumes.namespace})
