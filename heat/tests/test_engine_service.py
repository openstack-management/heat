#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import datetime
import uuid

import eventlet
from eventlet import event as grevent
import mock
import mox
from oslo_config import cfg
from oslo_messaging.rpc import dispatcher
from oslo_serialization import jsonutils as json
import six

from heat.common import context
from heat.common import exception
from heat.common import identifier
from heat.common import messaging
from heat.common import service_utils
from heat.common import template_format
from heat.engine import dependencies
from heat.engine import environment
from heat.engine import properties
from heat.engine import resource as res
from heat.engine.resources.aws.ec2 import instance as instances
from heat.engine import service
from heat.engine import service_stack_watch
from heat.engine import stack as parser
from heat.engine import stack_lock
from heat.engine import template as templatem
from heat.engine import watchrule
from heat.engine import worker
from heat.objects import event as event_object
from heat.objects import resource as resource_objects
from heat.objects import service as service_objects
from heat.objects import stack as stack_object
from heat.objects import stack_lock as stack_lock_object
from heat.objects import sync_point as sync_point_object
from heat.objects import watch_data as watch_data_object
from heat.objects import watch_rule as watch_rule_object
from heat.openstack.common import threadgroup
from heat.rpc import api as rpc_api
from heat.rpc import worker_api
from heat.tests import common
from heat.tests.engine import tools
from heat.tests import generic_resource as generic_rsrc
from heat.tests.nova import fakes as fakes_nova
from heat.tests import utils

cfg.CONF.import_opt('engine_life_check_timeout', 'heat.common.config')
cfg.CONF.import_opt('enable_stack_abandon', 'heat.common.config')


string_template_five = '''
heat_template_version: 2013-05-23
description: Random String templates

parameters:
    salt:
        type: string
        default: "quickbrownfox"

resources:
    A:
        type: OS::Heat::RandomString
        properties:
            salt: {get_param: salt}

    B:
        type: OS::Heat::RandomString
        properties:
            salt: {get_param: salt}

    C:
        type: OS::Heat::RandomString
        depends_on: [A, B]
        properties:
            salt: {get_param: salt}

    D:
        type: OS::Heat::RandomString
        depends_on: C
        properties:
            salt: {get_param: salt}

    E:
        type: OS::Heat::RandomString
        depends_on: C
        properties:
            salt: {get_param: salt}
'''

string_template_five_update = '''
heat_template_version: 2013-05-23
description: Random String templates

parameters:
    salt:
        type: string
        default: "quickbrownfox123"

resources:
    A:
        type: OS::Heat::RandomString
        properties:
            salt: {get_param: salt}

    B:
        type: OS::Heat::RandomString
        properties:
            salt: {get_param: salt}

    F:
        type: OS::Heat::RandomString
        depends_on: [A, B]
        properties:
            salt: {get_param: salt}

    G:
        type: OS::Heat::RandomString
        depends_on: F
        properties:
            salt: {get_param: salt}

    H:
        type: OS::Heat::RandomString
        depends_on: F
        properties:
            salt: {get_param: salt}
'''

empty_template = '''
heat_template_version: 2013-05-23
description: Empty Template
'''

wp_template_no_default = '''
{
  "AWSTemplateFormatVersion" : "2010-09-09",
  "Description" : "WordPress",
  "Parameters" : {
    "KeyName" : {
      "Description" : "KeyName",
      "Type" : "String"
    }
  },
  "Resources" : {
    "WebServer": {
      "Type": "AWS::EC2::Instance",
      "Properties": {
        "ImageId" : "F17-x86_64-gold",
        "InstanceType"   : "m1.large",
        "KeyName"        : "test",
        "UserData"       : "wordpress"
      }
    }
  }
}
'''

policy_template = '''
{
  "AWSTemplateFormatVersion" : "2010-09-09",
  "Description" : "alarming",
  "Resources" : {
    "WebServerScaleDownPolicy" : {
      "Type" : "AWS::AutoScaling::ScalingPolicy",
      "Properties" : {
        "AdjustmentType" : "ChangeInCapacity",
        "AutoScalingGroupName" : "",
        "Cooldown" : "60",
        "ScalingAdjustment" : "-1"
      }
    }
  }
}
'''

user_policy_template = '''
{
  "AWSTemplateFormatVersion" : "2010-09-09",
  "Description" : "Just a User",
  "Parameters" : {},
  "Resources" : {
    "CfnUser" : {
      "Type" : "AWS::IAM::User",
      "Properties" : {
        "Policies" : [ { "Ref": "WebServerAccessPolicy"} ]
      }
    },
    "WebServerAccessPolicy" : {
      "Type" : "OS::Heat::AccessPolicy",
      "Properties" : {
        "AllowedResources" : [ "WebServer" ]
      }
    },
    "HostKeys" : {
      "Type" : "AWS::IAM::AccessKey",
      "Properties" : {
        "UserName" : {"Ref": "CfnUser"}
      }
    },
    "WebServer": {
      "Type": "AWS::EC2::Instance",
      "Properties": {
        "ImageId" : "F17-x86_64-gold",
        "InstanceType"   : "m1.large",
        "KeyName"        : "test",
        "UserData"       : "wordpress"
      }
    }
  }
}
'''

server_config_template = '''
heat_template_version: 2013-05-23
resources:
  WebServer:
    type: OS::Nova::Server
'''


class StackConvergenceCreateUpdateDeleteTest(common.HeatTestCase):
    def setUp(self):
        super(StackConvergenceCreateUpdateDeleteTest, self).setUp()
        cfg.CONF.set_override('convergence_engine', True)

    def test_conv_wordpress_single_instance_stack_create(self):
        stack = tools.get_stack('test_stack', utils.dummy_context(),
                                convergence=True)
        stack.converge_stack(template=stack.t, action=stack.CREATE)
        self.assertIsNone(stack.ext_rsrcs_db)
        self.assertEqual('Dependencies([((1, True), None)])',
                         repr(stack.convergence_dependencies))

        stack_db = stack_object.Stack.get_by_id(stack.context, stack.id)
        self.assertIsNotNone(stack_db.current_traversal)
        self.assertIsNotNone(stack_db.raw_template_id)

        self.assertIsNone(stack_db.prev_raw_template_id)

        self.assertEqual(stack_db.convergence, True)
        self.assertEqual({'edges': [[[1, True], None]]}, stack_db.current_deps)

    def test_conv_string_five_instance_stack_create(self):
        stack = tools.get_stack('test_stack', utils.dummy_context(),
                                template=string_template_five,
                                convergence=True)
        stack.converge_stack(template=stack.t, action=stack.CREATE)
        self.assertIsNone(stack.ext_rsrcs_db)
        self.assertEqual('Dependencies(['
                         '((3, True), (5, True)), '
                         '((3, True), (4, True)), '
                         '((1, True), (3, True)), '
                         '((2, True), (3, True))])',
                         repr(stack.convergence_dependencies))

        stack_db = stack_object.Stack.get_by_id(stack.context, stack.id)
        self.assertIsNotNone(stack_db.current_traversal)
        self.assertIsNotNone(stack_db.raw_template_id)
        self.assertIsNone(stack_db.prev_raw_template_id)
        self.assertEqual(stack_db.convergence, True)
        self.assertEqual(sorted([[[3, True], [5, True]],    # C, A
                                 [[3, True], [4, True]],    # C, B
                                 [[1, True], [3, True]],    # E, C
                                 [[2, True], [3, True]]]),  # D, C
                         sorted(stack_db.current_deps['edges']))

        # check if needed_by is stored properly
        expected_needed_by = {'A': [3], 'B': [3],
                              'C': [1, 2],
                              'D': [], 'E': []}
        rsrcs_db = resource_objects.Resource.get_all_by_stack(
            stack_db._context, stack_db.id
        )
        self.assertEqual(5, len(rsrcs_db))
        for rsrc_name, rsrc_obj in rsrcs_db.items():
            self.assertEqual(sorted(expected_needed_by[rsrc_name]),
                             sorted(rsrc_obj.needed_by))
            self.assertEqual(stack_db.raw_template_id,
                             rsrc_obj.current_template_id)

        # check if sync_points were stored
        for entity_id in [5, 4, 3, 2, 1, stack_db.id]:
            sync_point = sync_point_object.SyncPoint.get_by_key(
                stack_db._context, entity_id, stack_db.current_traversal, True
            )
            self.assertIsNotNone(sync_point)
            self.assertEqual(stack_db.id, sync_point.stack_id)

    def test_conv_string_five_instance_stack_update(self):
        stack = tools.get_stack('test_stack', utils.dummy_context(),
                                template=string_template_five,
                                convergence=True)
        # create stack
        stack.converge_stack(template=stack.t, action=stack.CREATE)

        curr_stack_db = stack_object.Stack.get_by_id(stack.context, stack.id)
        curr_stack = parser.Stack.load(curr_stack_db._context,
                                       stack=curr_stack_db)
        # update stack with new template
        t2 = template_format.parse(string_template_five_update)
        template2 = templatem.Template(
            t2, env=environment.Environment({'KeyName2': 'test2'}))
        curr_stack.converge_stack(template=template2, action=stack.UPDATE)

        self.assertIsNotNone(curr_stack.ext_rsrcs_db)
        self.assertEqual('Dependencies(['
                         '((7, True), (8, True)), '
                         '((8, True), (5, True)), '
                         '((8, True), (4, True)), '
                         '((6, True), (8, True)), '
                         '((3, False), (2, False)), '
                         '((3, False), (1, False)), '
                         '((5, False), (3, False)), '
                         '((5, False), (5, True)), '
                         '((4, False), (3, False)), '
                         '((4, False), (4, True))])',
                         repr(curr_stack.convergence_dependencies))

        stack_db = stack_object.Stack.get_by_id(curr_stack.context,
                                                curr_stack.id)
        self.assertIsNotNone(stack_db.raw_template_id)
        self.assertIsNotNone(stack_db.current_traversal)
        self.assertIsNotNone(stack_db.prev_raw_template_id)
        self.assertEqual(True, stack_db.convergence)
        self.assertEqual(sorted([[[7, True], [8, True]],
                                 [[8, True], [5, True]],
                                 [[8, True], [4, True]],
                                 [[6, True], [8, True]],
                                 [[3, False], [2, False]],
                                 [[3, False], [1, False]],
                                 [[5, False], [3, False]],
                                 [[5, False], [5, True]],
                                 [[4, False], [3, False]],
                                 [[4, False], [4, True]]]),
                         sorted(stack_db.current_deps['edges']))
        '''
        To visualize:

        G(7, True)       H(6, True)
            \                 /
              \             /           B(4, False)   A(5, False)
                \         /               /       \  /    /
                  \     /            /           /
               F(8, True)       /             /     \  /
                    /  \    /             /     C(3, False)
                  /      \            /            /    \
                /     /    \      /
              /    /         \ /                /          \
        B(4, True)      A(5, True)       D(2, False)    E(1, False)

        Leaves are at the bottom
        '''

        # check if needed_by are stored properly
        # For A & B:
        # needed_by=C, F
        # TODO(later): when worker is implemented test for current_template_id
        # Also test for requires

        expected_needed_by = {'A': [3, 8], 'B': [3, 8],
                              'C': [1, 2],
                              'D': [], 'E': [],
                              'F': [6, 7],
                              'G': [], 'H': []}
        rsrcs_db = resource_objects.Resource.get_all_by_stack(
            stack_db._context, stack_db.id
        )
        self.assertEqual(8, len(rsrcs_db))
        for rsrc_name, rsrc_obj in rsrcs_db.items():
            self.assertEqual(sorted(expected_needed_by[rsrc_name]),
                             sorted(rsrc_obj.needed_by))

        # check if sync_points are created for forward traversal
        # [F, H, G, A, B, Stack]
        for entity_id in [8, 7, 6, 5, 4, stack_db.id]:
            sync_point = sync_point_object.SyncPoint.get_by_key(
                stack_db._context, entity_id, stack_db.current_traversal, True
            )
            self.assertIsNotNone(sync_point)
            self.assertEqual(stack_db.id, sync_point.stack_id)

        # check if sync_points are created for cleanup traversal
        # [A, B, C, D, E]
        for entity_id in [5, 4, 3, 2, 1]:
            sync_point = sync_point_object.SyncPoint.get_by_key(
                stack_db._context, entity_id, stack_db.current_traversal, False
            )
            self.assertIsNotNone(sync_point)
            self.assertEqual(stack_db.id, sync_point.stack_id)

    def test_conv_empty_template_stack_update_delete(self):
        stack = tools.get_stack('test_stack', utils.dummy_context(),
                                template=string_template_five,
                                convergence=True)
        # create stack
        stack.converge_stack(template=stack.t, action=stack.CREATE)

        # update stack with new template
        t2 = template_format.parse(empty_template)
        template2 = templatem.Template(
            t2, env=environment.Environment({'KeyName2': 'test2'}))

        curr_stack_db = stack_object.Stack.get_by_id(stack.context, stack.id)
        curr_stack = parser.Stack.load(curr_stack_db._context,
                                       stack=curr_stack_db)
        curr_stack.converge_stack(template=template2, action=stack.DELETE)

        self.assertIsNotNone(curr_stack.ext_rsrcs_db)
        self.assertEqual('Dependencies(['
                         '((3, False), (2, False)), '
                         '((3, False), (1, False)), '
                         '((5, False), (3, False)), '
                         '((4, False), (3, False))])',
                         repr(curr_stack.convergence_dependencies))

        stack_db = stack_object.Stack.get_by_id(curr_stack.context,
                                                curr_stack.id)
        self.assertIsNotNone(stack_db.current_traversal)
        self.assertIsNotNone(stack_db.prev_raw_template_id)
        self.assertEqual(sorted([[[3, False], [2, False]],
                                 [[3, False], [1, False]],
                                 [[5, False], [3, False]],
                                 [[4, False], [3, False]]]),
                         sorted(stack_db.current_deps['edges']))

        # TODO(later): when worker is implemented test for current_template_id
        # Also test for requires
        expected_needed_by = {'A': [3], 'B': [3],
                              'C': [1, 2],
                              'D': [], 'E': []}
        rsrcs_db = resource_objects.Resource.get_all_by_stack(
            stack_db._context, stack_db.id
        )
        self.assertEqual(5, len(rsrcs_db))
        for rsrc_name, rsrc_obj in rsrcs_db.items():
            self.assertEqual(sorted(expected_needed_by[rsrc_name]),
                             sorted(rsrc_obj.needed_by))

        # check if sync_points are created for cleanup traversal
        # [A, B, C, D, E, Stack]
        for entity_id in [5, 4, 3, 2, 1, stack_db.id]:
            sync_point = sync_point_object.SyncPoint.get_by_key(
                stack_db._context, entity_id, stack_db.current_traversal, False
            )
            self.assertIsNotNone(sync_point)
            self.assertEqual(stack_db.id, sync_point.stack_id)


class StackCreateTest(common.HeatTestCase):
    def setUp(self):
        super(StackCreateTest, self).setUp()

    def test_wordpress_single_instance_stack_create(self):
        stack = tools.get_stack('test_stack', utils.dummy_context())
        tools.setup_mocks(self.m, stack)
        self.m.ReplayAll()
        stack.store()
        stack.create()

        self.assertIsNotNone(stack['WebServer'])
        self.assertTrue(stack['WebServer'].resource_id > 0)
        self.assertNotEqual(stack['WebServer'].ipaddress, '0.0.0.0')

    def test_wordpress_single_instance_stack_adopt(self):
        t = template_format.parse(tools.wp_template)
        template = templatem.Template(t)
        ctx = utils.dummy_context()
        adopt_data = {
            'resources': {
                'WebServer': {
                    'resource_id': 'test-res-id'
                }
            }
        }
        stack = parser.Stack(ctx,
                             'test_stack',
                             template,
                             adopt_stack_data=adopt_data)

        tools.setup_mocks(self.m, stack)
        self.m.ReplayAll()
        stack.store()
        stack.adopt()

        self.assertIsNotNone(stack['WebServer'])
        self.assertEqual('test-res-id', stack['WebServer'].resource_id)
        self.assertEqual((stack.ADOPT, stack.COMPLETE), stack.state)

    def test_wordpress_single_instance_stack_adopt_fail(self):
        t = template_format.parse(tools.wp_template)
        template = templatem.Template(t)
        ctx = utils.dummy_context()
        adopt_data = {
            'resources': {
                'WebServer1': {
                    'resource_id': 'test-res-id'
                }
            }
        }
        stack = parser.Stack(ctx,
                             'test_stack',
                             template,
                             adopt_stack_data=adopt_data)

        tools.setup_mocks(self.m, stack)
        self.m.ReplayAll()
        stack.store()
        stack.adopt()
        self.assertIsNotNone(stack['WebServer'])
        expected = ('Resource ADOPT failed: Exception: Resource ID was not'
                    ' provided.')
        self.assertEqual(expected, stack.status_reason)
        self.assertEqual((stack.ADOPT, stack.FAILED), stack.state)

    def test_wordpress_single_instance_stack_delete(self):
        ctx = utils.dummy_context()
        stack = tools.get_stack('test_stack', ctx)
        fc = tools.setup_mocks(self.m, stack, mock_keystone=False)
        self.m.ReplayAll()
        stack_id = stack.store()
        stack.create()

        db_s = stack_object.Stack.get_by_id(ctx, stack_id)
        self.assertIsNotNone(db_s)

        self.assertIsNotNone(stack['WebServer'])
        self.assertTrue(stack['WebServer'].resource_id > 0)

        self.m.StubOutWithMock(fc.client, 'get_servers_9999')
        get = fc.client.get_servers_9999
        get().AndRaise(fakes_nova.fake_exception())
        mox.Replay(get)
        stack.delete()

        rsrc = stack['WebServer']
        self.assertEqual((rsrc.DELETE, rsrc.COMPLETE), rsrc.state)
        self.assertEqual((stack.DELETE, stack.COMPLETE), rsrc.state)
        self.assertIsNone(stack_object.Stack.get_by_id(ctx, stack_id))

        db_s.refresh()
        self.assertEqual('DELETE', db_s.action)
        self.assertEqual('COMPLETE', db_s.status, )


class StackServiceCreateUpdateDeleteTest(common.HeatTestCase):

    def setUp(self):
        super(StackServiceCreateUpdateDeleteTest, self).setUp()
        self.ctx = utils.dummy_context()
        self.man = service.EngineService('a-host', 'a-topic')
        self.man.create_periodic_tasks()

    def _test_stack_create(self, stack_name):
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'

        stack = tools.get_stack(stack_name, self.ctx)

        self.m.StubOutWithMock(templatem, 'Template')
        self.m.StubOutWithMock(environment, 'Environment')
        self.m.StubOutWithMock(parser, 'Stack')

        templatem.Template(template, files=None,
                           env=stack.env).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(self.ctx, stack.name,
                     stack.t, owner_id=None,
                     nested_depth=0, user_creds_id=None,
                     stack_user_project_id=None,
                     convergence=False,
                     parent_resource=None).AndReturn(stack)

        self.m.StubOutWithMock(stack, 'validate')
        stack.validate().AndReturn(None)

        self.m.StubOutWithMock(threadgroup, 'ThreadGroup')
        threadgroup.ThreadGroup().AndReturn(tools.DummyThreadGroup())

        self.m.ReplayAll()

        result = self.man.create_stack(self.ctx, stack_name,
                                       template, params, None, {})
        self.assertEqual(stack.identifier(), result)
        self.assertIsInstance(result, dict)
        self.assertTrue(result['stack_id'])
        self.m.VerifyAll()

    def test_stack_create(self):
        stack_name = 'service_create_test_stack'
        self._test_stack_create(stack_name)

    def test_stack_create_equals_max_per_tenant(self):
        cfg.CONF.set_override('max_stacks_per_tenant', 1)
        stack_name = 'service_create_test_stack_equals_max'
        self._test_stack_create(stack_name)

    def test_stack_create_exceeds_max_per_tenant(self):
        cfg.CONF.set_override('max_stacks_per_tenant', 0)
        stack_name = 'service_create_test_stack_exceeds_max'
        ex = self.assertRaises(dispatcher.ExpectedException,
                               self._test_stack_create, stack_name)
        self.assertEqual(exception.RequestLimitExceeded, ex.exc_info[0])
        self.assertIn("You have reached the maximum stacks per tenant",
                      six.text_type(ex.exc_info[1]))

    def test_stack_create_verify_err(self):
        stack_name = 'service_create_verify_err_test_stack'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'

        stack = tools.get_stack(stack_name, self.ctx)

        self.m.StubOutWithMock(templatem, 'Template')
        self.m.StubOutWithMock(environment, 'Environment')
        self.m.StubOutWithMock(parser, 'Stack')

        templatem.Template(template, files=None,
                           env=stack.env).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(self.ctx, stack.name,
                     stack.t,
                     owner_id=None,
                     nested_depth=0,
                     user_creds_id=None,
                     stack_user_project_id=None,
                     convergence=False,
                     parent_resource=None).AndReturn(stack)

        self.m.StubOutWithMock(stack, 'validate')
        stack.validate().AndRaise(exception.StackValidationFailed(
            message='fubar'))

        self.m.ReplayAll()

        ex = self.assertRaises(
            dispatcher.ExpectedException,
            self.man.create_stack,
            self.ctx, stack_name,
            template, params, None, {})
        self.assertEqual(exception.StackValidationFailed, ex.exc_info[0])
        self.m.VerifyAll()

    def _get_stack_adopt_data_and_template(self, environment=None):
        template = {
            "heat_template_version": "2013-05-23",
            "parameters": {"app_dbx": {"type": "string"}},
            "resources": {"res1": {"type": "GenericResourceType"}}}

        adopt_data = {
            "status": "COMPLETE",
            "name": "rtrove1",
            "environment": environment,
            "template": template,
            "action": "CREATE",
            "id": "8532f0d3-ea84-444e-b2bb-2543bb1496a4",
            "resources": {"res1": {
                    "status": "COMPLETE",
                    "name": "database_password",
                    "resource_id": "yBpuUROjfGQ2gKOD",
                    "action": "CREATE",
                    "type": "GenericResourceType",
                    "metadata": {}}}}
        return template, adopt_data

    def test_stack_adopt_with_params(self):
        cfg.CONF.set_override('enable_stack_adopt', True)
        environment = {'parameters': {"app_dbx": "test"}}
        template, adopt_data = self._get_stack_adopt_data_and_template(
            environment)
        res._register_class('GenericResourceType',
                            generic_rsrc.GenericResource)
        result = self.man.create_stack(self.ctx, "test_adopt_stack",
                                       template, {}, None,
                                       {'adopt_stack_data': str(adopt_data)})

        stack = stack_object.Stack.get_by_id(self.ctx, result['stack_id'])
        self.assertEqual(template, stack.raw_template.template)
        self.assertEqual(environment['parameters'],
                         stack.raw_template.environment['parameters'])

    def test_stack_adopt_saves_input_params(self):
        cfg.CONF.set_override('enable_stack_adopt', True)
        environment = {'parameters': {"app_dbx": "foo"}}
        input_params = {
            "parameters": {"app_dbx": "bar"}
        }
        template, adopt_data = self._get_stack_adopt_data_and_template(
            environment)
        res._register_class('GenericResourceType',
                            generic_rsrc.GenericResource)
        result = self.man.create_stack(self.ctx, "test_adopt_stack",
                                       template, input_params, None,
                                       {'adopt_stack_data': str(adopt_data)})

        stack = stack_object.Stack.get_by_id(self.ctx, result['stack_id'])
        self.assertEqual(template, stack.raw_template.template)
        self.assertEqual(input_params['parameters'],
                         stack.raw_template.environment['parameters'])

    def test_stack_adopt_stack_state(self):
        cfg.CONF.set_override('enable_stack_adopt', True)
        env = {'parameters': {"app_dbx": "test"}}
        template, adopt_data = self._get_stack_adopt_data_and_template(
            env)
        res._register_class('GenericResourceType',
                            generic_rsrc.GenericResource)
        result = self.man.create_stack(self.ctx, "test_adopt_stack",
                                       template, {}, None,
                                       {'adopt_stack_data': str(adopt_data)})

        stack = stack_object.Stack.get_by_id(self.ctx, result['stack_id'])
        self.assertEqual((parser.Stack.ADOPT, parser.Stack.IN_PROGRESS),
                         (stack.action, stack.status))

    def test_stack_adopt_disabled(self):
        # to test disable stack adopt
        cfg.CONF.set_override('enable_stack_adopt', False)
        environment = {'parameters': {"app_dbx": "test"}}
        template, adopt_data = self._get_stack_adopt_data_and_template(
            environment)
        res._register_class('GenericResourceType',
                            generic_rsrc.GenericResource)
        ex = self.assertRaises(
            dispatcher.ExpectedException,
            self.man.create_stack,
            self.ctx, "test_adopt_stack_disabled",
            template, {}, None,
            {'adopt_stack_data': str(adopt_data)})
        self.assertEqual(exception.NotSupported, ex.exc_info[0])
        self.assertIn('Stack Adopt', six.text_type(ex.exc_info[1]))

    def test_stack_create_invalid_stack_name(self):
        stack_name = 'service_create_test_stack_invalid_name'
        stack = tools.get_stack('test_stack', self.ctx)

        self.assertRaises(dispatcher.ExpectedException,
                          self.man.create_stack,
                          self.ctx, stack_name, stack.t.t, {}, None, {})

    def test_stack_create_invalid_resource_name(self):
        stack_name = 'service_create_test_stack_invalid_res'
        stack = tools.get_stack(stack_name, self.ctx)
        tmpl = dict(stack.t)
        tmpl['resources']['Web/Server'] = tmpl['resources']['WebServer']
        del tmpl['resources']['WebServer']

        self.assertRaises(dispatcher.ExpectedException,
                          self.man.create_stack,
                          self.ctx, stack_name,
                          stack.t.t, {}, None, {})

    def test_stack_create_AuthorizationFailure(self):
        stack_name = 'service_create_test_stack_AuthorizationFailure'
        stack = tools.get_stack(stack_name, self.ctx)
        self.m.StubOutWithMock(parser.Stack, 'create_stack_user_project_id')
        parser.Stack.create_stack_user_project_id().AndRaise(
            exception.AuthorizationFailure)
        self.assertRaises(dispatcher.ExpectedException,
                          self.man.create_stack,
                          self.ctx, stack_name,
                          stack.t.t, {}, None, {})

    def test_stack_create_no_credentials(self):
        cfg.CONF.set_default('deferred_auth_method', 'password')
        stack_name = 'test_stack_create_no_credentials'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'

        stack = tools.get_stack(stack_name, self.ctx)
        # force check for credentials on create
        stack['WebServer'].requires_deferred_auth = True

        self.m.StubOutWithMock(templatem, 'Template')
        self.m.StubOutWithMock(environment, 'Environment')
        self.m.StubOutWithMock(parser, 'Stack')

        ctx_no_pwd = utils.dummy_context(password=None)
        ctx_no_user = utils.dummy_context(user=None)

        templatem.Template(template, files=None,
                           env=stack.env).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(ctx_no_pwd, stack.name,
                     stack.t, owner_id=None,
                     nested_depth=0, user_creds_id=None,
                     stack_user_project_id=None,
                     convergence=False,
                     parent_resource=None).AndReturn(stack)

        templatem.Template(template, files=None,
                           env=stack.env).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(ctx_no_user, stack.name,
                     stack.t, owner_id=None,
                     nested_depth=0, user_creds_id=None,
                     stack_user_project_id=None,
                     convergence=False,
                     parent_resource=None).AndReturn(stack)

        self.m.ReplayAll()

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.man.create_stack,
                               ctx_no_pwd, stack_name,
                               template, params, None, {}, None)
        self.assertEqual(exception.MissingCredentialError, ex.exc_info[0])
        self.assertEqual(
            'Missing required credential: X-Auth-Key',
            six.text_type(ex.exc_info[1]))

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.man.create_stack,
                               ctx_no_user, stack_name,
                               template, params, None, {})
        self.assertEqual(exception.MissingCredentialError, ex.exc_info[0])
        self.assertEqual(
            'Missing required credential: X-Auth-User',
            six.text_type(ex.exc_info[1]))

    @mock.patch.object(stack_object.Stack, 'count_total_resources')
    def test_stack_create_total_resources_equals_max(self, ctr):
        stack_name = 'service_create_stack_total_resources_equals_max'
        params = {}
        res._register_class('GenericResourceType',
                            generic_rsrc.GenericResource)
        tpl = {'HeatTemplateFormatVersion': '2012-12-12',
               'Resources': {
                   'A': {'Type': 'GenericResourceType'},
                   'B': {'Type': 'GenericResourceType'},
                   'C': {'Type': 'GenericResourceType'}}}

        template = templatem.Template(tpl)
        stack = parser.Stack(self.ctx, stack_name, template)
        ctr.return_value = 3

        self.m.StubOutWithMock(templatem, 'Template')
        self.m.StubOutWithMock(environment, 'Environment')
        self.m.StubOutWithMock(parser, 'Stack')

        templatem.Template(template, files=None,
                           env=stack.env).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(self.ctx, stack.name,
                     stack.t,
                     owner_id=None,
                     nested_depth=0,
                     user_creds_id=None,
                     stack_user_project_id=None,
                     convergence=False,
                     parent_resource=None).AndReturn(stack)

        self.m.ReplayAll()

        cfg.CONF.set_override('max_resources_per_stack', 3)

        result = self.man.create_stack(self.ctx, stack_name, template, params,
                                       None, {})
        self.m.VerifyAll()
        self.assertEqual(stack.identifier(), result)
        root_stack_id = stack.root_stack_id()
        self.assertEqual(3, stack.total_resources(root_stack_id))
        self.man.thread_group_mgr.groups[stack.id].wait()
        stack.delete()

    def test_stack_create_total_resources_exceeds_max(self):
        stack_name = 'service_create_stack_total_resources_exceeds_max'
        params = {}
        res._register_class('GenericResourceType',
                            generic_rsrc.GenericResource)
        tpl = {'HeatTemplateFormatVersion': '2012-12-12',
               'Resources': {
                   'A': {'Type': 'GenericResourceType'},
                   'B': {'Type': 'GenericResourceType'},
                   'C': {'Type': 'GenericResourceType'}}}
        cfg.CONF.set_override('max_resources_per_stack', 2)
        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.man.create_stack, self.ctx, stack_name,
                               tpl, params, None, {})
        self.assertEqual(exception.RequestLimitExceeded, ex.exc_info[0])
        self.assertIn(exception.StackResourceLimitExceeded.msg_fmt,
                      six.text_type(ex.exc_info[1]))

    def test_stack_validate(self):
        stack_name = 'service_create_test_validate'
        stack = tools.get_stack(stack_name, self.ctx)
        tools.setup_mocks(self.m, stack, mock_image_constraint=False)
        resource = stack['WebServer']

        tools.setup_mock_for_image_constraint(self.m, 'CentOS 5.2')
        self.m.ReplayAll()

        resource.properties = properties.Properties(
            resource.properties_schema,
            {
                'ImageId': 'CentOS 5.2',
                'KeyName': 'test',
                'InstanceType': 'm1.large'
            },
            context=self.ctx)
        stack.validate()

        resource.properties = properties.Properties(
            resource.properties_schema,
            {
                'KeyName': 'test',
                'InstanceType': 'm1.large'
            },
            context=self.ctx)
        self.assertRaises(exception.StackValidationFailed, stack.validate)

    def test_stack_delete(self):
        stack_name = 'service_delete_test_stack'
        stack = tools.get_stack(stack_name, self.ctx)
        sid = stack.store()

        s = stack_object.Stack.get_by_id(self.ctx, sid)
        self.m.StubOutWithMock(parser.Stack, 'load')

        parser.Stack.load(self.ctx, stack=s).AndReturn(stack)
        self.m.ReplayAll()

        self.assertIsNone(self.man.delete_stack(self.ctx, stack.identifier()))
        self.man.thread_group_mgr.groups[sid].wait()
        self.m.VerifyAll()

    def test_stack_delete_nonexist(self):
        stack_name = 'service_delete_nonexist_test_stack'
        stack = tools.get_stack(stack_name, self.ctx)

        self.m.ReplayAll()

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.man.delete_stack,
                               self.ctx, stack.identifier())
        self.assertEqual(exception.StackNotFound, ex.exc_info[0])
        self.m.VerifyAll()

    def test_stack_delete_acquired_lock(self):
        stack_name = 'service_delete_test_stack'
        stack = tools.get_stack(stack_name, self.ctx)
        sid = stack.store()

        st = stack_object.Stack.get_by_id(self.ctx, sid)
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx, stack=st).MultipleTimes().AndReturn(stack)

        self.m.StubOutWithMock(stack_lock.StackLock, 'try_acquire')
        stack_lock.StackLock.try_acquire().AndReturn(self.man.engine_id)
        self.m.ReplayAll()

        self.assertIsNone(self.man.delete_stack(self.ctx, stack.identifier()))
        self.man.thread_group_mgr.groups[sid].wait()
        self.m.VerifyAll()

    def test_stack_delete_acquired_lock_stop_timers(self):
        stack_name = 'service_delete_test_stack'
        stack = tools.get_stack(stack_name, self.ctx)
        sid = stack.store()

        st = stack_object.Stack.get_by_id(self.ctx, sid)
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx, stack=st).MultipleTimes().AndReturn(stack)

        self.m.StubOutWithMock(stack_lock.StackLock, 'try_acquire')
        stack_lock.StackLock.try_acquire().AndReturn(self.man.engine_id)
        self.m.ReplayAll()

        self.man.thread_group_mgr.add_timer(stack.id, 'test')
        self.assertEqual(1, len(self.man.thread_group_mgr.groups[sid].timers))
        self.assertIsNone(self.man.delete_stack(self.ctx, stack.identifier()))
        self.assertEqual(0, len(self.man.thread_group_mgr.groups[sid].timers))
        self.man.thread_group_mgr.groups[sid].wait()
        self.m.VerifyAll()

    def test_stack_delete_current_engine_active_lock(self):
        self.man.start()
        stack_name = 'service_delete_test_stack'
        stack = tools.get_stack(stack_name, self.ctx)
        sid = stack.store()

        # Insert a fake lock into the db
        stack_lock_object.StackLock.create(stack.id, self.man.engine_id)

        # Create a fake ThreadGroup too
        self.man.thread_group_mgr.groups[stack.id] = tools.DummyThreadGroup()

        st = stack_object.Stack.get_by_id(self.ctx, sid)
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx, stack=st).MultipleTimes().AndReturn(stack)

        self.m.StubOutWithMock(stack_lock.StackLock, 'try_acquire')
        stack_lock.StackLock.try_acquire().AndReturn(self.man.engine_id)
        # this is to simulate lock release on DummyThreadGroup stop
        self.m.StubOutWithMock(stack_lock.StackLock, 'acquire')
        stack_lock.StackLock.acquire().AndReturn(None)

        self.m.StubOutWithMock(self.man.thread_group_mgr, 'stop')
        self.man.thread_group_mgr.stop(stack.id).AndReturn(None)
        self.m.ReplayAll()

        self.assertIsNone(self.man.delete_stack(self.ctx, stack.identifier()))
        self.m.VerifyAll()

    def test_stack_delete_other_engine_active_lock_failed(self):
        self.man.start()
        stack_name = 'service_delete_test_stack'
        stack = tools.get_stack(stack_name, self.ctx)
        sid = stack.store()

        # Insert a fake lock into the db
        stack_lock_object.StackLock.create(stack.id, "other-engine-fake-uuid")

        st = stack_object.Stack.get_by_id(self.ctx, sid)
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx, stack=st).AndReturn(stack)

        self.m.StubOutWithMock(stack_lock.StackLock, 'try_acquire')
        stack_lock.StackLock.try_acquire().AndReturn("other-engine-fake-uuid")

        self.m.StubOutWithMock(stack_lock.StackLock, 'engine_alive')
        stack_lock.StackLock.engine_alive(
            self.ctx, "other-engine-fake-uuid").AndReturn(True)

        self.m.StubOutWithMock(self.man, '_remote_call')
        self.man._remote_call(
            self.ctx, 'other-engine-fake-uuid', 'stop_stack',
            stack_identity=mox.IgnoreArg()
        ).AndReturn(False)
        self.m.ReplayAll()

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.man.delete_stack,
                               self.ctx, stack.identifier())
        self.assertEqual(exception.StopActionFailed, ex.exc_info[0])
        self.m.VerifyAll()

    def test_stack_delete_other_engine_active_lock_succeeded(self):
        self.man.start()
        stack_name = 'service_delete_test_stack'
        stack = tools.get_stack(stack_name, self.ctx)
        sid = stack.store()

        # Insert a fake lock into the db
        stack_lock_object.StackLock.create(stack.id, "other-engine-fake-uuid")

        st = stack_object.Stack.get_by_id(self.ctx, sid)
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx, stack=st).MultipleTimes().AndReturn(stack)

        self.m.StubOutWithMock(stack_lock.StackLock, 'try_acquire')
        stack_lock.StackLock.try_acquire().AndReturn("other-engine-fake-uuid")

        self.m.StubOutWithMock(stack_lock.StackLock, 'engine_alive')
        stack_lock.StackLock.engine_alive(
            self.ctx, "other-engine-fake-uuid").AndReturn(True)

        self.m.StubOutWithMock(self.man, '_remote_call')
        self.man._remote_call(
            self.ctx, 'other-engine-fake-uuid', 'stop_stack',
            stack_identity=mox.IgnoreArg()).AndReturn(None)

        self.m.StubOutWithMock(stack_lock.StackLock, 'acquire')
        stack_lock.StackLock.acquire().AndReturn(None)
        self.m.ReplayAll()

        self.assertIsNone(self.man.delete_stack(self.ctx, stack.identifier()))
        self.man.thread_group_mgr.groups[sid].wait()
        self.m.VerifyAll()

    def test_stack_delete_other_dead_engine_active_lock(self):
        stack_name = 'service_delete_test_stack'
        stack = tools.get_stack(stack_name, self.ctx)
        sid = stack.store()

        # Insert a fake lock into the db
        stack_lock_object.StackLock.create(stack.id, "other-engine-fake-uuid")

        st = stack_object.Stack.get_by_id(self.ctx, sid)
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx, stack=st).MultipleTimes().AndReturn(stack)

        self.m.StubOutWithMock(stack_lock.StackLock, 'try_acquire')
        stack_lock.StackLock.try_acquire().AndReturn("other-engine-fake-uuid")

        self.m.StubOutWithMock(stack_lock.StackLock, 'engine_alive')
        stack_lock.StackLock.engine_alive(
            self.ctx, "other-engine-fake-uuid").AndReturn(False)

        self.m.StubOutWithMock(stack_lock.StackLock, 'acquire')
        stack_lock.StackLock.acquire().AndReturn(None)
        self.m.ReplayAll()

        self.assertIsNone(self.man.delete_stack(self.ctx, stack.identifier()))
        self.man.thread_group_mgr.groups[sid].wait()
        self.m.VerifyAll()

    def _stub_update_mocks(self, stack_to_load, stack_to_return):
        self.m.StubOutWithMock(parser, 'Stack')
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx, stack=stack_to_load
                          ).AndReturn(stack_to_return)

        self.m.StubOutWithMock(templatem, 'Template')
        self.m.StubOutWithMock(environment, 'Environment')

    def test_stack_update(self):
        stack_name = 'service_update_test_stack'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'
        old_stack = tools.get_stack(stack_name, self.ctx)
        sid = old_stack.store()
        old_stack.set_stack_user_project_id('1234')
        s = stack_object.Stack.get_by_id(self.ctx, sid)

        stack = tools.get_stack(stack_name, self.ctx)

        self._stub_update_mocks(s, old_stack)

        templatem.Template(template, files=None,
                           env=stack.env).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(self.ctx, stack.name,
                     stack.t,
                     convergence=False,
                     current_traversal=None,
                     prev_raw_template_id=None,
                     current_deps=None,
                     disable_rollback=True,
                     nested_depth=0,
                     owner_id=None,
                     parent_resource=None,
                     stack_user_project_id='1234',
                     strict_validate=True,
                     tenant_id='test_tenant_id',
                     timeout_mins=60,
                     user_creds_id=u'1',
                     username='test_username').AndReturn(stack)

        self.m.StubOutWithMock(stack, 'validate')
        stack.validate().AndReturn(None)

        evt_mock = self.m.CreateMockAnything()
        self.m.StubOutWithMock(grevent, 'Event')
        grevent.Event().AndReturn(evt_mock)
        self.m.StubOutWithMock(threadgroup, 'ThreadGroup')
        threadgroup.ThreadGroup().AndReturn(tools.DummyThreadGroup())

        self.m.ReplayAll()

        api_args = {'timeout_mins': 60}
        result = self.man.update_stack(self.ctx, old_stack.identifier(),
                                       template, params, None, api_args)
        self.assertEqual(old_stack.identifier(), result)
        self.assertIsInstance(result, dict)
        self.assertTrue(result['stack_id'])
        self.assertEqual([evt_mock], self.man.thread_group_mgr.events[sid])
        self.m.VerifyAll()

    def test_stack_update_existing_parameters(self):
        '''Use a template with default parameter and no input parameter
        then update with a template without default and no input
        parameter, using the existing parameter.
        '''
        stack_name = 'service_update_test_stack_existing_parameters'
        no_params = {}
        with_params = {'KeyName': 'foo'}

        old_stack = tools.get_stack(stack_name, self.ctx, with_params=False)
        sid = old_stack.store()
        old_stack.set_stack_user_project_id('1234')
        s = stack_object.Stack.get_by_id(self.ctx, sid)

        t = template_format.parse(wp_template_no_default)
        env = environment.Environment({'parameters': with_params,
                                       'resource_registry': {'rsc': 'test'}})
        template = templatem.Template(t, env=env)
        stack = parser.Stack(self.ctx, stack_name, template)

        self._stub_update_mocks(s, old_stack)

        templatem.Template(wp_template_no_default,
                           files=None, env=old_stack.env).AndReturn(stack.t)
        environment.Environment(no_params).AndReturn(old_stack.env)
        parser.Stack(self.ctx, stack.name,
                     stack.t,
                     convergence=False, current_traversal=None,
                     prev_raw_template_id=None, current_deps=None,
                     disable_rollback=True, nested_depth=0,
                     owner_id=None, parent_resource=None,
                     stack_user_project_id='1234',
                     strict_validate=True,
                     tenant_id='test_tenant_id', timeout_mins=60,
                     user_creds_id=u'1',
                     username='test_username').AndReturn(stack)

        self.m.StubOutWithMock(stack, 'validate')
        stack.validate().AndReturn(None)

        evt_mock = self.m.CreateMockAnything()
        self.m.StubOutWithMock(grevent, 'Event')
        grevent.Event().AndReturn(evt_mock)
        self.m.StubOutWithMock(threadgroup, 'ThreadGroup')
        threadgroup.ThreadGroup().AndReturn(tools.DummyThreadGroup())

        self.m.ReplayAll()

        api_args = {rpc_api.PARAM_TIMEOUT: 60,
                    rpc_api.PARAM_EXISTING: True}
        result = self.man.update_stack(self.ctx, old_stack.identifier(),
                                       wp_template_no_default, no_params,
                                       None, api_args)
        self.assertEqual(old_stack.identifier(), result)
        self.assertIsInstance(result, dict)
        self.assertTrue(result['stack_id'])
        self.assertEqual([evt_mock], self.man.thread_group_mgr.events[sid])
        self.m.VerifyAll()

    def test_stack_update_reuses_api_params(self):
        stack_name = 'service_update_test_stack'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'

        old_stack = tools.get_stack(stack_name, self.ctx)
        old_stack.timeout_mins = 1
        old_stack.disable_rollback = False
        sid = old_stack.store()
        old_stack.set_stack_user_project_id('1234')
        s = stack_object.Stack.get_by_id(self.ctx, sid)

        stack = tools.get_stack(stack_name, self.ctx)

        self._stub_update_mocks(s, old_stack)

        templatem.Template(template, files=None,
                           env=stack.env).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(self.ctx, stack.name,
                     stack.t,
                     convergence=False, current_traversal=None,
                     prev_raw_template_id=None, current_deps=None,
                     disable_rollback=False, nested_depth=0,
                     owner_id=None, parent_resource=None,
                     stack_user_project_id='1234',
                     strict_validate=True,
                     tenant_id='test_tenant_id', timeout_mins=1,
                     user_creds_id=u'1',
                     username='test_username').AndReturn(stack)

        self.m.StubOutWithMock(stack, 'validate')
        stack.validate().AndReturn(None)

        self.m.StubOutWithMock(threadgroup, 'ThreadGroup')
        threadgroup.ThreadGroup().AndReturn(tools.DummyThreadGroup())

        self.m.ReplayAll()

        api_args = {}
        result = self.man.update_stack(self.ctx, old_stack.identifier(),
                                       template, params, None, api_args)
        self.assertEqual(old_stack.identifier(), result)
        self.assertIsInstance(result, dict)
        self.assertTrue(result['stack_id'])
        self.m.VerifyAll()

    def test_stack_cancel_update_same_engine(self):
        stack_name = 'service_update_cancel_test_stack'
        old_stack = tools.get_stack(stack_name, self.ctx)
        old_stack.state_set(old_stack.UPDATE, old_stack.IN_PROGRESS,
                            'test_override')
        old_stack.disable_rollback = False
        old_stack.store()
        load_mock = self.patchobject(parser.Stack, 'load')
        load_mock.return_value = old_stack
        lock_mock = self.patchobject(stack_lock.StackLock, 'try_acquire')
        lock_mock.return_value = self.man.engine_id
        self.patchobject(self.man.thread_group_mgr, 'send')
        self.man.stack_cancel_update(self.ctx, old_stack.identifier())
        self.man.thread_group_mgr.send.assert_called_once_with(old_stack.id,
                                                               'cancel')

    def test_stack_cancel_update_different_engine(self):
        stack_name = 'service_update_cancel_test_stack'
        old_stack = tools.get_stack(stack_name, self.ctx)
        old_stack.state_set(old_stack.UPDATE, old_stack.IN_PROGRESS,
                            'test_override')
        old_stack.disable_rollback = False
        old_stack.store()
        load_mock = self.patchobject(parser.Stack, 'load')
        load_mock.return_value = old_stack
        lock_mock = self.patchobject(stack_lock.StackLock, 'try_acquire')
        another_engine_has_lock = str(uuid.uuid4())
        lock_mock.return_value = another_engine_has_lock
        self.patchobject(stack_lock.StackLock,
                         'engine_alive').return_value(True)
        self.man.listener = mock.Mock()
        self.man.listener.SEND = 'send'
        self.man._client = messaging.get_rpc_client(
            version=self.man.RPC_API_VERSION)
        # In fact the another engine is not alive, so the call will timeout
        self.assertRaises(dispatcher.ExpectedException,
                          self.man.stack_cancel_update,
                          self.ctx, old_stack.identifier())

    def test_stack_cancel_update_wrong_state_fails(self):
        stack_name = 'service_update_cancel_test_stack'
        old_stack = tools.get_stack(stack_name, self.ctx)
        old_stack.state_set(old_stack.UPDATE, old_stack.COMPLETE,
                            'test_override')
        old_stack.store()
        load_mock = self.patchobject(parser.Stack, 'load')
        load_mock.return_value = old_stack

        ex = self.assertRaises(
            dispatcher.ExpectedException,
            self.man.stack_cancel_update, self.ctx, old_stack.identifier())

        self.assertEqual(exception.NotSupported, ex.exc_info[0])
        self.assertIn("Cancelling update when stack is "
                      "('UPDATE', 'COMPLETE')",
                      six.text_type(ex.exc_info[1]))

    @mock.patch.object(stack_object.Stack, 'count_total_resources')
    def test_stack_update_equals(self, ctr):
        stack_name = 'test_stack_update_equals_resource_limit'
        params = {}
        res._register_class('GenericResourceType',
                            generic_rsrc.GenericResource)
        tpl = {'HeatTemplateFormatVersion': '2012-12-12',
               'Resources': {
                   'A': {'Type': 'GenericResourceType'},
                   'B': {'Type': 'GenericResourceType'},
                   'C': {'Type': 'GenericResourceType'}}}

        template = templatem.Template(tpl)

        old_stack = parser.Stack(self.ctx, stack_name, template)
        sid = old_stack.store()
        old_stack.set_stack_user_project_id('1234')
        s = stack_object.Stack.get_by_id(self.ctx, sid)
        ctr.return_value = 3

        stack = parser.Stack(self.ctx, stack_name, template)

        self._stub_update_mocks(s, old_stack)

        templatem.Template(template, files=None,
                           env=stack.env).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(self.ctx, stack.name,
                     stack.t,
                     convergence=False, current_traversal=None,
                     prev_raw_template_id=None, current_deps=None,
                     disable_rollback=True, nested_depth=0,
                     owner_id=None, parent_resource=None,
                     stack_user_project_id='1234', strict_validate=True,
                     tenant_id='test_tenant_id',
                     timeout_mins=60, user_creds_id=u'1',
                     username='test_username').AndReturn(stack)

        self.m.StubOutWithMock(stack, 'validate')
        stack.validate().AndReturn(None)

        self.m.StubOutWithMock(threadgroup, 'ThreadGroup')
        threadgroup.ThreadGroup().AndReturn(tools.DummyThreadGroup())

        self.m.ReplayAll()

        cfg.CONF.set_override('max_resources_per_stack', 3)

        api_args = {'timeout_mins': 60}
        result = self.man.update_stack(self.ctx, old_stack.identifier(),
                                       template, params, None, api_args)
        self.assertEqual(old_stack.identifier(), result)
        self.assertIsInstance(result, dict)
        self.assertTrue(result['stack_id'])
        root_stack_id = old_stack.root_stack_id()
        self.assertEqual(3, old_stack.total_resources(root_stack_id))
        self.m.VerifyAll()

    def test_stack_update_stack_id_equal(self):
        stack_name = 'test_stack_update_stack_id_equal'
        res._register_class('ResourceWithPropsType',
                            generic_rsrc.ResourceWithProps)
        tpl = {
            'HeatTemplateFormatVersion': '2012-12-12',
            'Resources': {
                'A': {
                    'Type': 'ResourceWithPropsType',
                    'Properties': {
                        'Foo': {'Ref': 'AWS::StackId'}
                    }
                }
            }
        }

        template = templatem.Template(tpl)

        create_stack = parser.Stack(self.ctx, stack_name, template)
        sid = create_stack.store()
        create_stack.create()
        self.assertEqual((create_stack.CREATE, create_stack.COMPLETE),
                         create_stack.state)

        s = stack_object.Stack.get_by_id(self.ctx, sid)

        old_stack = parser.Stack.load(self.ctx, stack=s)

        self.assertEqual((old_stack.CREATE, old_stack.COMPLETE),
                         old_stack.state)
        self.assertEqual(create_stack.identifier().arn(),
                         old_stack['A'].properties['Foo'])

        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(
            self.ctx,
            stack=s).AndReturn(old_stack)

        self.m.ReplayAll()

        result = self.man.update_stack(self.ctx, create_stack.identifier(),
                                       tpl, {}, None, {})

        self.man.thread_group_mgr.groups[sid].wait()

        self.assertEqual((old_stack.UPDATE, old_stack.COMPLETE),
                         old_stack.state)
        self.assertEqual(create_stack.identifier(), result)
        self.assertIsNotNone(create_stack.identifier().stack_id)
        self.assertEqual(create_stack.identifier().arn(),
                         old_stack['A'].properties['Foo'])

        self.assertEqual(create_stack['A'].id, old_stack['A'].id)
        self.man.thread_group_mgr.groups[sid].wait()

        self.m.VerifyAll()

    def test_stack_update_exceeds_resource_limit(self):
        stack_name = 'test_stack_update_exceeds_resource_limit'
        params = {}
        res._register_class('GenericResourceType',
                            generic_rsrc.GenericResource)
        tpl = {'HeatTemplateFormatVersion': '2012-12-12',
               'Resources': {
                   'A': {'Type': 'GenericResourceType'},
                   'B': {'Type': 'GenericResourceType'},
                   'C': {'Type': 'GenericResourceType'}}}

        template = templatem.Template(tpl)
        old_stack = parser.Stack(self.ctx, stack_name, template)
        sid = old_stack.store()
        self.assertIsNotNone(sid)

        cfg.CONF.set_override('max_resources_per_stack', 2)

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.man.update_stack, self.ctx,
                               old_stack.identifier(), tpl, params,
                               None, {})
        self.assertEqual(exception.RequestLimitExceeded, ex.exc_info[0])
        self.assertIn(exception.StackResourceLimitExceeded.msg_fmt,
                      six.text_type(ex.exc_info[1]))

    def test_stack_update_verify_err(self):
        stack_name = 'service_update_verify_err_test_stack'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'

        old_stack = tools.get_stack(stack_name, self.ctx)
        old_stack.store()
        sid = old_stack.store()
        old_stack.set_stack_user_project_id('1234')
        s = stack_object.Stack.get_by_id(self.ctx, sid)
        stack = tools.get_stack(stack_name, self.ctx)

        self._stub_update_mocks(s, old_stack)

        templatem.Template(template, files=None,
                           env=stack.env).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(self.ctx, stack.name,
                     stack.t,
                     convergence=False, current_traversal=None,
                     prev_raw_template_id=None, current_deps=None,
                     disable_rollback=True, nested_depth=0,
                     owner_id=None, parent_resource=None,
                     stack_user_project_id='1234', strict_validate=True,
                     tenant_id='test_tenant_id',
                     timeout_mins=60, user_creds_id=u'1',
                     username='test_username').AndReturn(stack)

        self.m.StubOutWithMock(stack, 'validate')
        stack.validate().AndRaise(exception.StackValidationFailed(
            message='fubar'))

        self.m.ReplayAll()

        api_args = {'timeout_mins': 60}
        ex = self.assertRaises(
            dispatcher.ExpectedException,
            self.man.update_stack,
            self.ctx, old_stack.identifier(),
            template, params, None, api_args)
        self.assertEqual(exception.StackValidationFailed, ex.exc_info[0])
        self.m.VerifyAll()

    def test_stack_update_nonexist(self):
        stack_name = 'service_update_nonexist_test_stack'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'
        stack = tools.get_stack(stack_name, self.ctx)

        self.m.ReplayAll()

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.man.update_stack,
                               self.ctx, stack.identifier(), template,
                               params, None, {})
        self.assertEqual(exception.StackNotFound, ex.exc_info[0])
        self.m.VerifyAll()

    def test_stack_update_no_credentials(self):
        cfg.CONF.set_default('deferred_auth_method', 'password')
        stack_name = 'test_stack_update_no_credentials'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'

        old_stack = tools.get_stack(stack_name, self.ctx)
        # force check for credentials on create
        old_stack['WebServer'].requires_deferred_auth = True

        sid = old_stack.store()
        old_stack.set_stack_user_project_id('1234')
        s = stack_object.Stack.get_by_id(self.ctx, sid)

        self.ctx = utils.dummy_context(password=None)

        self.m.StubOutWithMock(self.man, '_get_stack')

        self.man._get_stack(self.ctx, old_stack.identifier()).AndReturn(s)

        self._stub_update_mocks(s, old_stack)

        templatem.Template(template, files=None,
                           env=old_stack.env).AndReturn(old_stack.t)
        environment.Environment(params).AndReturn(old_stack.env)
        parser.Stack(self.ctx, old_stack.name,
                     old_stack.t,
                     convergence=False,
                     current_traversal=None,
                     prev_raw_template_id=None,
                     current_deps=None,
                     disable_rollback=True,
                     nested_depth=0,
                     owner_id=None,
                     parent_resource=None,
                     stack_user_project_id='1234',
                     strict_validate=True,
                     tenant_id='test_tenant_id',
                     timeout_mins=60,
                     user_creds_id=u'1',
                     username='test_username').AndReturn(old_stack)

        self.m.ReplayAll()

        api_args = {'timeout_mins': 60}
        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.man.update_stack, self.ctx,
                               old_stack.identifier(),
                               template, params, None, api_args)
        self.assertEqual(exception.MissingCredentialError, ex.exc_info[0])
        self.assertEqual(
            'Missing required credential: X-Auth-Key',
            six.text_type(ex.exc_info[1]))

        self.m.VerifyAll()

    def test_validate_deferred_auth_context_trusts(self):
        stack = tools.get_stack('test_deferred_auth', self.ctx)
        stack['WebServer'].requires_deferred_auth = True
        ctx = utils.dummy_context(user=None, password=None)
        cfg.CONF.set_default('deferred_auth_method', 'trusts')

        # using trusts, no username or password required
        self.man._validate_deferred_auth_context(ctx, stack)

    def test_validate_deferred_auth_context_not_required(self):
        stack = tools.get_stack('test_deferred_auth', self.ctx)
        stack['WebServer'].requires_deferred_auth = False
        ctx = utils.dummy_context(user=None, password=None)
        cfg.CONF.set_default('deferred_auth_method', 'password')

        # stack performs no deferred operations, so no username or
        # password required
        self.man._validate_deferred_auth_context(ctx, stack)

    def test_validate_deferred_auth_context_missing_credentials(self):
        stack = tools.get_stack('test_deferred_auth', self.ctx)
        stack['WebServer'].requires_deferred_auth = True
        cfg.CONF.set_default('deferred_auth_method', 'password')

        # missing username
        ctx = utils.dummy_context(user=None)
        ex = self.assertRaises(exception.MissingCredentialError,
                               self.man._validate_deferred_auth_context,
                               ctx, stack)
        self.assertEqual('Missing required credential: X-Auth-User',
                         six.text_type(ex))

        # missing password
        ctx = utils.dummy_context(password=None)
        ex = self.assertRaises(exception.MissingCredentialError,
                               self.man._validate_deferred_auth_context,
                               ctx, stack)
        self.assertEqual('Missing required credential: X-Auth-Key',
                         six.text_type(ex))


class StackConvergenceServiceCreateUpdateTest(common.HeatTestCase):

    def setUp(self):
        super(StackConvergenceServiceCreateUpdateTest, self).setUp()
        cfg.CONF.set_override('convergence_engine', True)
        self.ctx = utils.dummy_context()
        self.man = service.EngineService('a-host', 'a-topic')
        self.man.create_periodic_tasks()

    def _stub_update_mocks(self, stack_to_load, stack_to_return):
        self.m.StubOutWithMock(parser, 'Stack')
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx, stack=stack_to_load
                          ).AndReturn(stack_to_return)

        self.m.StubOutWithMock(templatem, 'Template')
        self.m.StubOutWithMock(environment, 'Environment')

    def _test_stack_create_convergence(self, stack_name):
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'

        stack = tools.get_stack(stack_name, self.ctx,
                                template=string_template_five,
                                convergence=True)

        self.m.StubOutWithMock(templatem, 'Template')
        self.m.StubOutWithMock(environment, 'Environment')
        self.m.StubOutWithMock(parser, 'Stack')

        templatem.Template(template, files=None,
                           env=stack.env).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(self.ctx, stack.name,
                     stack.t, owner_id=None,
                     parent_resource=None,
                     nested_depth=0, user_creds_id=None,
                     stack_user_project_id=None,
                     convergence=True).AndReturn(stack)

        self.m.StubOutWithMock(stack, 'validate')
        stack.validate().AndReturn(None)

        self.m.ReplayAll()

        # TODO(later): Remove exception once convergence is supported.
        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.man.create_stack, self.ctx, stack_name,
                               template, params, None, {})
        self.assertEqual(exception.NotSupported, ex.exc_info[0])
        self.assertEqual('Convergence engine is not supported.',
                         six.text_type(ex.exc_info[1]))
        self.m.VerifyAll()

    def test_stack_create_enabled_convergence_engine(self):
        stack_name = 'service_create_test_stack'
        self._test_stack_create_convergence(stack_name)

    def test_stack_update_enabled_convergence_engine(self):
        stack_name = 'service_update_test_stack'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'
        old_stack = tools.get_stack(stack_name, self.ctx,
                                    template=string_template_five,
                                    convergence=True)
        sid = old_stack.store()
        s = stack_object.Stack.get_by_id(self.ctx, sid)

        stack = tools.get_stack(stack_name, self.ctx,
                                template=string_template_five_update,
                                convergence=True)

        self._stub_update_mocks(s, old_stack)

        templatem.Template(template, files=None,
                           env=stack.env).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(self.ctx, stack.name,
                     stack.t,
                     owner_id=old_stack.owner_id,
                     nested_depth=old_stack.nested_depth,
                     user_creds_id=old_stack.user_creds_id,
                     stack_user_project_id=old_stack.stack_user_project_id,
                     timeout_mins=60,
                     disable_rollback=True,
                     parent_resource=None,
                     strict_validate=True,
                     tenant_id=old_stack.tenant_id,
                     username=old_stack.username,
                     convergence=old_stack.convergence,
                     current_traversal=old_stack.current_traversal,
                     prev_raw_template_id=old_stack.prev_raw_template_id,
                     current_deps=old_stack.current_deps).AndReturn(stack)

        self.m.StubOutWithMock(stack, 'validate')
        stack.validate().AndReturn(None)

        self.m.ReplayAll()

        api_args = {'timeout_mins': 60}
        result = self.man.update_stack(self.ctx, old_stack.identifier(),
                                       template, params, None, api_args)
        self.assertEqual(old_stack.convergence, True)
        self.assertEqual(old_stack.identifier(), result)
        self.assertIsInstance(result, dict)
        self.assertTrue(result['stack_id'])
        self.m.VerifyAll()


class StackServiceAuthorizeTest(common.HeatTestCase):

    def setUp(self):
        super(StackServiceAuthorizeTest, self).setUp()

        self.ctx = utils.dummy_context(tenant_id='stack_service_test_tenant')
        self.eng = service.EngineService('a-host', 'a-topic')
        self.eng.engine_id = 'engine-fake-uuid'
        cfg.CONF.set_default('heat_stack_user_role', 'stack_user_role')
        res._register_class('ResourceWithPropsType',
                            generic_rsrc.ResourceWithProps)

    @tools.stack_context('service_authorize_stack_user_nocreds_test_stack')
    def test_stack_authorize_stack_user_nocreds(self):
        self.assertFalse(self.eng._authorize_stack_user(self.ctx,
                                                        self.stack,
                                                        'foo'))

    @tools.stack_context('service_authorize_user_attribute_error_test_stack')
    def test_stack_authorize_stack_user_attribute_error(self):
        self.m.StubOutWithMock(json, 'loads')
        json.loads(None).AndRaise(AttributeError)
        self.m.ReplayAll()
        self.assertFalse(self.eng._authorize_stack_user(self.ctx,
                                                        self.stack,
                                                        'foo'))
        self.m.VerifyAll()

    @tools.stack_context('service_authorize_stack_user_type_error_test_stack')
    def test_stack_authorize_stack_user_type_error(self):
        self.m.StubOutWithMock(json, 'loads')
        json.loads(mox.IgnoreArg()).AndRaise(TypeError)
        self.m.ReplayAll()

        self.assertFalse(self.eng._authorize_stack_user(self.ctx,
                                                        self.stack,
                                                        'foo'))

        self.m.VerifyAll()

    def test_stack_authorize_stack_user(self):
        self.ctx = utils.dummy_context()
        self.ctx.aws_creds = '{"ec2Credentials": {"access": "4567"}}'
        stack_name = 'stack_authorize_stack_user'
        stack = tools.get_stack(stack_name, self.ctx, user_policy_template)
        self.stack = stack
        fc = tools.setup_mocks(self.m, stack)
        self.m.StubOutWithMock(fc.client, 'get_servers_9999')
        get = fc.client.get_servers_9999
        get().AndRaise(fakes_nova.fake_exception())

        self.m.ReplayAll()
        stack.store()
        stack.create()

        self.assertTrue(self.eng._authorize_stack_user(
            self.ctx, self.stack, 'WebServer'))

        self.assertFalse(self.eng._authorize_stack_user(
            self.ctx, self.stack, 'CfnUser'))

        self.assertFalse(self.eng._authorize_stack_user(
            self.ctx, self.stack, 'NoSuchResource'))

        self.stack.delete()
        self.m.VerifyAll()

    def test_stack_authorize_stack_user_user_id(self):
        self.ctx = utils.dummy_context(user_id=str(uuid.uuid4()))
        stack_name = 'stack_authorize_stack_user_user_id'
        stack = tools.get_stack(stack_name, self.ctx, server_config_template)
        self.stack = stack

        def handler(resource_name):
            return resource_name == 'WebServer'

        self.stack.register_access_allowed_handler(self.ctx.user_id, handler)

        # matching credential_id and resource_name
        self.assertTrue(self.eng._authorize_stack_user(
            self.ctx, self.stack, 'WebServer'))

        # not matching resource_name
        self.assertFalse(self.eng._authorize_stack_user(
            self.ctx, self.stack, 'NoSuchResource'))

        # not matching credential_id
        self.ctx.user_id = str(uuid.uuid4())
        self.assertFalse(self.eng._authorize_stack_user(
            self.ctx, self.stack, 'WebServer'))


class StackServiceTest(common.HeatTestCase):

    def setUp(self):
        super(StackServiceTest, self).setUp()

        self.ctx = utils.dummy_context(tenant_id='stack_service_test_tenant')
        self.eng = service.EngineService('a-host', 'a-topic')
        self.eng.create_periodic_tasks()
        self.eng.engine_id = 'engine-fake-uuid'
        cfg.CONF.set_default('heat_stack_user_role', 'stack_user_role')
        res._register_class('ResourceWithPropsType',
                            generic_rsrc.ResourceWithProps)

    def test_make_sure_rpc_version(self):
        self.assertEqual(
            '1.9',
            service.EngineService.RPC_API_VERSION,
            ('RPC version is changed, please update this test to new version '
             'and make sure additional test cases are added for RPC APIs '
             'added in new version'))

    @mock.patch.object(service_stack_watch.StackWatch, 'start_watch_task')
    @mock.patch.object(stack_object.Stack, 'get_all')
    @mock.patch.object(service.service.Service, 'start')
    def test_start_watches_all_stacks(self, mock_super_start, mock_get_all,
                                      start_watch_task):
        s1 = mock.Mock(id=1)
        s2 = mock.Mock(id=2)
        mock_get_all.return_value = [s1, s2]
        start_watch_task.return_value = None

        self.eng.thread_group_mgr = None
        self.eng.create_periodic_tasks()

        mock_get_all.assert_called_once_with(mock.ANY, tenant_safe=False,
                                             show_hidden=True)
        calls = start_watch_task.call_args_list
        self.assertEqual(2, start_watch_task.call_count)
        self.assertIn(mock.call(1, mock.ANY), calls)
        self.assertIn(mock.call(2, mock.ANY), calls)

    @tools.stack_context('service_identify_test_stack', False)
    def test_stack_identify(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)

        self.m.ReplayAll()
        identity = self.eng.identify_stack(self.ctx, self.stack.name)
        self.assertEqual(self.stack.identifier(), identity)

        self.m.VerifyAll()

    @tools.stack_context('ef0c41a4-644f-447c-ad80-7eecb0becf79', False)
    def test_stack_identify_by_name_in_uuid(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)

        self.m.ReplayAll()
        identity = self.eng.identify_stack(self.ctx, self.stack.name)
        self.assertEqual(self.stack.identifier(), identity)

        self.m.VerifyAll()

    @tools.stack_context('service_identify_uuid_test_stack', False)
    def test_stack_identify_uuid(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)

        self.m.ReplayAll()
        identity = self.eng.identify_stack(self.ctx, self.stack.id)
        self.assertEqual(self.stack.identifier(), identity)

        self.m.VerifyAll()

    def test_stack_identify_nonexist(self):
        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.identify_stack, self.ctx, 'wibble')
        self.assertEqual(exception.StackNotFound, ex.exc_info[0])

    @tools.stack_context('service_create_existing_test_stack', False)
    def test_stack_create_existing(self):
        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.create_stack, self.ctx,
                               self.stack.name, self.stack.t.t, {}, None, {})
        self.assertEqual(exception.StackExists, ex.exc_info[0])

    @tools.stack_context('service_name_tenants_test_stack', False)
    def test_stack_by_name_tenants(self):
        self.assertEqual(
            self.stack.id,
            stack_object.Stack.get_by_name(self.ctx, self.stack.name).id)
        ctx2 = utils.dummy_context(tenant_id='stack_service_test_tenant2')
        self.assertIsNone(stack_object.Stack.get_by_name(
            ctx2,
            self.stack.name))

    @tools.stack_context('service_event_list_test_stack')
    def test_stack_event_list(self):
        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        s = stack_object.Stack.get_by_id(self.ctx, self.stack.id)
        service.EngineService._get_stack(self.ctx,
                                         self.stack.identifier(),
                                         show_deleted=True).AndReturn(s)
        self.m.ReplayAll()

        events = self.eng.list_events(self.ctx, self.stack.identifier())

        self.assertEqual(4, len(events))
        for ev in events:
            self.assertIn('event_identity', ev)
            self.assertIsInstance(ev['event_identity'], dict)
            self.assertTrue(ev['event_identity']['path'].rsplit('/', 1)[1])

            self.assertIn('resource_name', ev)
            self.assertIn(ev['resource_name'],
                          ('service_event_list_test_stack', 'WebServer'))

            self.assertIn('physical_resource_id', ev)

            self.assertIn('resource_properties', ev)
            # Big long user data field.. it mentions 'wordpress'
            # a few times so this should work.
            if ev.get('resource_properties'):
                user_data = ev['resource_properties']['UserData']
                self.assertIn('wordpress', user_data)
                self.assertEqual('F17-x86_64-gold',
                                 ev['resource_properties']['ImageId'])
                self.assertEqual('m1.large',
                                 ev['resource_properties']['InstanceType'])

            self.assertEqual('CREATE', ev['resource_action'])
            self.assertIn(ev['resource_status'], ('IN_PROGRESS', 'COMPLETE'))

            self.assertIn('resource_status_reason', ev)
            self.assertIn(ev['resource_status_reason'],
                          ('state changed',
                           'Stack CREATE started',
                           'Stack CREATE completed successfully'))

            self.assertIn('resource_type', ev)
            self.assertIn(ev['resource_type'],
                          ('AWS::EC2::Instance', 'OS::Heat::Stack'))

            self.assertIn('stack_identity', ev)

            self.assertIn('stack_name', ev)
            self.assertEqual(self.stack.name, ev['stack_name'])

            self.assertIn('event_time', ev)

        self.m.VerifyAll()

    @tools.stack_context('event_list_deleted_stack')
    def test_stack_event_list_deleted_resource(self):
        res._register_class('GenericResourceType',
                            generic_rsrc.GenericResource)

        thread = self.m.CreateMockAnything()
        thread.link(mox.IgnoreArg()).AndReturn(None)
        thread.link(mox.IgnoreArg(), self.stack.id,
                    mox.IgnoreArg()).AndReturn(None)

        def run(stack_id, func, *args, **kwargs):
            func(*args)
            return thread
        self.eng.thread_group_mgr.start = run

        new_tmpl = {'HeatTemplateFormatVersion': '2012-12-12',
                    'Resources': {'AResource': {'Type':
                                                'GenericResourceType'}}}

        self.m.StubOutWithMock(instances.Instance, 'handle_delete')
        instances.Instance.handle_delete()

        self.m.ReplayAll()

        result = self.eng.update_stack(self.ctx, self.stack.identifier(),
                                       new_tmpl, None, None, {})

        # The self.stack reference needs to be updated. Since the underlying
        # stack is updated in update_stack, the original reference is now
        # pointing to an orphaned stack object.
        self.stack = parser.Stack.load(self.ctx, stack_id=result['stack_id'])

        self.assertEqual(result, self.stack.identifier())
        self.assertIsInstance(result, dict)
        self.assertTrue(result['stack_id'])
        events = self.eng.list_events(self.ctx, self.stack.identifier())

        self.assertEqual(9, len(events))

        for ev in events:
            self.assertIn('event_identity', ev)
            self.assertIsInstance(ev['event_identity'], dict)
            self.assertTrue(ev['event_identity']['path'].rsplit('/', 1)[1])

            self.assertIn('resource_name', ev)
            self.assertIn('physical_resource_id', ev)
            self.assertIn('resource_properties', ev)
            self.assertIn('resource_status_reason', ev)

            self.assertIn(ev['resource_action'],
                          ('CREATE', 'UPDATE', 'DELETE'))
            self.assertIn(ev['resource_status'], ('IN_PROGRESS', 'COMPLETE'))

            self.assertIn('resource_type', ev)
            self.assertIn(ev['resource_type'], ('AWS::EC2::Instance',
                                                'GenericResourceType',
                                                'OS::Heat::Stack'))

            self.assertIn('stack_identity', ev)

            self.assertIn('stack_name', ev)
            self.assertEqual(self.stack.name, ev['stack_name'])

            self.assertIn('event_time', ev)

        self.m.VerifyAll()

    @tools.stack_context('service_event_list_test_stack')
    def test_stack_event_list_by_tenant(self):
        events = self.eng.list_events(self.ctx, None)

        self.assertEqual(4, len(events))
        for ev in events:
            self.assertIn('event_identity', ev)
            self.assertIsInstance(ev['event_identity'], dict)
            self.assertTrue(ev['event_identity']['path'].rsplit('/', 1)[1])

            self.assertIn('resource_name', ev)
            self.assertIn(ev['resource_name'],
                          ('WebServer', 'service_event_list_test_stack'))

            self.assertIn('physical_resource_id', ev)

            self.assertIn('resource_properties', ev)
            # Big long user data field.. it mentions 'wordpress'
            # a few times so this should work.
            if ev.get('resource_properties'):
                user_data = ev['resource_properties']['UserData']
                self.assertIn('wordpress', user_data)
                self.assertEqual('F17-x86_64-gold',
                                 ev['resource_properties']['ImageId'])
                self.assertEqual('m1.large',
                                 ev['resource_properties']['InstanceType'])

            self.assertEqual('CREATE', ev['resource_action'])
            self.assertIn(ev['resource_status'], ('IN_PROGRESS', 'COMPLETE'))

            self.assertIn('resource_status_reason', ev)
            self.assertIn(ev['resource_status_reason'],
                          ('state changed',
                           'Stack CREATE started',
                           'Stack CREATE completed successfully'))

            self.assertIn('resource_type', ev)
            self.assertIn(ev['resource_type'],
                          ('AWS::EC2::Instance', 'OS::Heat::Stack'))

            self.assertIn('stack_identity', ev)

            self.assertIn('stack_name', ev)
            self.assertEqual(self.stack.name, ev['stack_name'])

            self.assertIn('event_time', ev)

        self.m.VerifyAll()

    @mock.patch.object(event_object.Event, 'get_all_by_stack')
    @mock.patch.object(service.EngineService, '_get_stack')
    def test_stack_events_list_passes_marker_and_filters(self,
                                                         mock_get_stack,
                                                         mock_events_get_all):
        limit = object()
        marker = object()
        sort_keys = object()
        sort_dir = object()
        filters = object()
        s = mock.Mock(id=1)
        mock_get_stack.return_value = s
        self.eng.list_events(self.ctx, 1, limit=limit,
                             marker=marker, sort_keys=sort_keys,
                             sort_dir=sort_dir, filters=filters)
        mock_events_get_all.assert_called_once_with(self.ctx,
                                                    1,
                                                    limit=limit,
                                                    sort_keys=sort_keys,
                                                    marker=marker,
                                                    sort_dir=sort_dir,
                                                    filters=filters)

    @mock.patch.object(event_object.Event, 'get_all_by_tenant')
    def test_tenant_events_list_passes_marker_and_filters(
            self, mock_tenant_events_get_all):
        limit = object()
        marker = object()
        sort_keys = object()
        sort_dir = object()
        filters = object()

        self.eng.list_events(self.ctx, None, limit=limit,
                             marker=marker, sort_keys=sort_keys,
                             sort_dir=sort_dir, filters=filters)
        mock_tenant_events_get_all.assert_called_once_with(self.ctx,
                                                           limit=limit,
                                                           sort_keys=sort_keys,
                                                           marker=marker,
                                                           sort_dir=sort_dir,
                                                           filters=filters)

    @tools.stack_context('service_list_all_test_stack')
    def test_stack_list_all(self):
        self.m.StubOutWithMock(parser.Stack, '_from_db')
        parser.Stack._from_db(
            self.ctx, mox.IgnoreArg(),
            resolve_data=False
        ).AndReturn(self.stack)

        self.m.ReplayAll()
        sl = self.eng.list_stacks(self.ctx)

        self.assertEqual(1, len(sl))
        for s in sl:
            self.assertIn('creation_time', s)
            self.assertIn('updated_time', s)
            self.assertIn('stack_identity', s)
            self.assertIsNotNone(s['stack_identity'])
            self.assertIn('stack_name', s)
            self.assertEqual(self.stack.name, s['stack_name'])
            self.assertIn('stack_status', s)
            self.assertIn('stack_status_reason', s)
            self.assertIn('description', s)
            self.assertIn('WordPress', s['description'])

        self.m.VerifyAll()

    @mock.patch.object(stack_object.Stack, 'get_all')
    def test_stack_list_passes_marker_info(self, mock_stack_get_all):
        limit = object()
        marker = object()
        sort_keys = object()
        sort_dir = object()
        self.eng.list_stacks(self.ctx, limit=limit, marker=marker,
                             sort_keys=sort_keys, sort_dir=sort_dir)
        mock_stack_get_all.assert_called_once_with(self.ctx,
                                                   limit,
                                                   sort_keys,
                                                   marker,
                                                   sort_dir,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   )

    @mock.patch.object(stack_object.Stack, 'get_all')
    def test_stack_list_passes_filtering_info(self, mock_stack_get_all):
        filters = {'foo': 'bar'}
        self.eng.list_stacks(self.ctx, filters=filters)
        mock_stack_get_all.assert_called_once_with(mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   filters,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   )

    @mock.patch.object(stack_object.Stack, 'get_all')
    def test_stack_list_tenant_safe_defaults_to_true(self, mock_stack_get_all):
        self.eng.list_stacks(self.ctx)
        mock_stack_get_all.assert_called_once_with(mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   True,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   )

    @mock.patch.object(stack_object.Stack, 'get_all')
    def test_stack_list_passes_tenant_safe_info(self, mock_stack_get_all):
        self.eng.list_stacks(self.ctx, tenant_safe=False)
        mock_stack_get_all.assert_called_once_with(mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   False,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   )

    @mock.patch.object(stack_object.Stack, 'get_all')
    def test_stack_list_show_nested(self, mock_stack_get_all):
        self.eng.list_stacks(self.ctx, show_nested=True)
        mock_stack_get_all.assert_called_once_with(mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   True,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   )

    @mock.patch.object(stack_object.Stack, 'get_all')
    def test_stack_list_show_deleted(self, mock_stack_get_all):
        self.eng.list_stacks(self.ctx, show_deleted=True)
        mock_stack_get_all.assert_called_once_with(mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   True,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   )

    @mock.patch.object(stack_object.Stack, 'get_all')
    def test_stack_list_show_hidden(self, mock_stack_get_all):
        self.eng.list_stacks(self.ctx, show_hidden=True)
        mock_stack_get_all.assert_called_once_with(mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   True,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   )

    @mock.patch.object(stack_object.Stack, 'get_all')
    def test_stack_list_tags(self, mock_stack_get_all):
        self.eng.list_stacks(self.ctx, tags=['foo', 'bar'])
        mock_stack_get_all.assert_called_once_with(mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   ['foo', 'bar'],
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   )

    @mock.patch.object(stack_object.Stack, 'get_all')
    def test_stack_list_tags_any(self, mock_stack_get_all):
        self.eng.list_stacks(self.ctx, tags_any=['foo', 'bar'])
        mock_stack_get_all.assert_called_once_with(mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   ['foo', 'bar'],
                                                   mock.ANY,
                                                   mock.ANY,
                                                   )

    @mock.patch.object(stack_object.Stack, 'get_all')
    def test_stack_list_not_tags(self, mock_stack_get_all):
        self.eng.list_stacks(self.ctx, not_tags=['foo', 'bar'])
        mock_stack_get_all.assert_called_once_with(mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   ['foo', 'bar'],
                                                   mock.ANY,
                                                   )

    @mock.patch.object(stack_object.Stack, 'get_all')
    def test_stack_list_not_tags_any(self, mock_stack_get_all):
        self.eng.list_stacks(self.ctx, not_tags_any=['foo', 'bar'])
        mock_stack_get_all.assert_called_once_with(mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   mock.ANY,
                                                   ['foo', 'bar'],
                                                   )

    @mock.patch.object(stack_object.Stack, 'count_all')
    def test_count_stacks_passes_filter_info(self, mock_stack_count_all):
        self.eng.count_stacks(self.ctx, filters={'foo': 'bar'})
        mock_stack_count_all.assert_called_once_with(mock.ANY,
                                                     filters={'foo': 'bar'},
                                                     tenant_safe=mock.ANY,
                                                     show_deleted=False,
                                                     show_nested=False,
                                                     show_hidden=False,
                                                     tags=None,
                                                     tags_any=None,
                                                     not_tags=None,
                                                     not_tags_any=None)

    @mock.patch.object(stack_object.Stack, 'count_all')
    def test_count_stacks_tenant_safe_default_true(self, mock_stack_count_all):
        self.eng.count_stacks(self.ctx)
        mock_stack_count_all.assert_called_once_with(mock.ANY,
                                                     filters=mock.ANY,
                                                     tenant_safe=True,
                                                     show_deleted=False,
                                                     show_nested=False,
                                                     show_hidden=False,
                                                     tags=None,
                                                     tags_any=None,
                                                     not_tags=None,
                                                     not_tags_any=None)

    @mock.patch.object(stack_object.Stack, 'count_all')
    def test_count_stacks_passes_tenant_safe_info(self, mock_stack_count_all):
        self.eng.count_stacks(self.ctx, tenant_safe=False)
        mock_stack_count_all.assert_called_once_with(mock.ANY,
                                                     filters=mock.ANY,
                                                     tenant_safe=False,
                                                     show_deleted=False,
                                                     show_nested=False,
                                                     show_hidden=False,
                                                     tags=None,
                                                     tags_any=None,
                                                     not_tags=None,
                                                     not_tags_any=None)

    @mock.patch.object(stack_object.Stack, 'count_all')
    def test_count_stacks_show_nested(self, mock_stack_count_all):
        self.eng.count_stacks(self.ctx, show_nested=True)
        mock_stack_count_all.assert_called_once_with(mock.ANY,
                                                     filters=mock.ANY,
                                                     tenant_safe=True,
                                                     show_deleted=False,
                                                     show_nested=True,
                                                     show_hidden=False,
                                                     tags=None,
                                                     tags_any=None,
                                                     not_tags=None,
                                                     not_tags_any=None)

    @mock.patch.object(stack_object.Stack, 'count_all')
    def test_count_stack_show_deleted(self, mock_stack_count_all):
        self.eng.count_stacks(self.ctx, show_deleted=True)
        mock_stack_count_all.assert_called_once_with(mock.ANY,
                                                     filters=mock.ANY,
                                                     tenant_safe=True,
                                                     show_deleted=True,
                                                     show_nested=False,
                                                     show_hidden=False,
                                                     tags=None,
                                                     tags_any=None,
                                                     not_tags=None,
                                                     not_tags_any=None)

    @mock.patch.object(stack_object.Stack, 'count_all')
    def test_count_stack_show_hidden(self, mock_stack_count_all):
        self.eng.count_stacks(self.ctx, show_hidden=True)
        mock_stack_count_all.assert_called_once_with(mock.ANY,
                                                     filters=mock.ANY,
                                                     tenant_safe=True,
                                                     show_deleted=False,
                                                     show_nested=False,
                                                     show_hidden=True,
                                                     tags=None,
                                                     tags_any=None,
                                                     not_tags=None,
                                                     not_tags_any=None)

    @tools.stack_context('service_abandon_stack')
    def test_abandon_stack(self):
        cfg.CONF.set_override('enable_stack_abandon', True)
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)
        expected_res = {
            u'WebServer': {
                'action': 'CREATE',
                'metadata': {},
                'name': u'WebServer',
                'resource_data': {},
                'resource_id': '9999',
                'status': 'COMPLETE',
                'type': u'AWS::EC2::Instance'}}
        self.m.ReplayAll()
        ret = self.eng.abandon_stack(self.ctx, self.stack.identifier())
        self.assertEqual(9, len(ret))
        self.assertEqual('CREATE', ret['action'])
        self.assertEqual('COMPLETE', ret['status'])
        self.assertEqual('service_abandon_stack', ret['name'])
        self.assertIn('id', ret)
        self.assertEqual(expected_res, ret['resources'])
        self.assertEqual(self.stack.t.t, ret['template'])
        self.assertIn('project_id', ret)
        self.assertIn('stack_user_project_id', ret)
        self.assertIn('environment', ret)
        self.m.VerifyAll()
        self.eng.thread_group_mgr.groups[self.stack.id].wait()

    def test_stack_describe_nonexistent(self):
        non_exist_identifier = identifier.HeatIdentifier(
            self.ctx.tenant_id, 'wibble',
            '18d06e2e-44d3-4bef-9fbf-52480d604b02')

        stack_not_found_exc = exception.StackNotFound(stack_name='test')
        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        service.EngineService._get_stack(
            self.ctx, non_exist_identifier,
            show_deleted=True).AndRaise(stack_not_found_exc)
        self.m.ReplayAll()

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.show_stack,
                               self.ctx, non_exist_identifier)
        self.assertEqual(exception.StackNotFound, ex.exc_info[0])
        self.m.VerifyAll()

    def test_stack_describe_bad_tenant(self):
        non_exist_identifier = identifier.HeatIdentifier(
            'wibble', 'wibble',
            '18d06e2e-44d3-4bef-9fbf-52480d604b02')

        invalid_tenant_exc = exception.InvalidTenant(target='test',
                                                     actual='test')
        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        service.EngineService._get_stack(
            self.ctx, non_exist_identifier,
            show_deleted=True).AndRaise(invalid_tenant_exc)
        self.m.ReplayAll()

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.show_stack,
                               self.ctx, non_exist_identifier)
        self.assertEqual(exception.InvalidTenant, ex.exc_info[0])

        self.m.VerifyAll()

    @tools.stack_context('service_describe_test_stack', False)
    def test_stack_describe(self):
        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        s = stack_object.Stack.get_by_id(self.ctx, self.stack.id)
        service.EngineService._get_stack(self.ctx,
                                         self.stack.identifier(),
                                         show_deleted=True).AndReturn(s)
        self.m.ReplayAll()

        sl = self.eng.show_stack(self.ctx, self.stack.identifier())

        self.assertEqual(1, len(sl))

        s = sl[0]
        self.assertIn('creation_time', s)
        self.assertIn('updated_time', s)
        self.assertIn('stack_identity', s)
        self.assertIsNotNone(s['stack_identity'])
        self.assertIn('stack_name', s)
        self.assertEqual(self.stack.name, s['stack_name'])
        self.assertIn('stack_status', s)
        self.assertIn('stack_status_reason', s)
        self.assertIn('description', s)
        self.assertIn('WordPress', s['description'])
        self.assertIn('parameters', s)

        self.m.VerifyAll()

    @tools.stack_context('service_describe_all_test_stack', False)
    def test_stack_describe_all(self):
        sl = self.eng.show_stack(self.ctx, None)

        self.assertEqual(1, len(sl))

        s = sl[0]
        self.assertIn('creation_time', s)
        self.assertIn('updated_time', s)
        self.assertIn('stack_identity', s)
        self.assertIsNotNone(s['stack_identity'])
        self.assertIn('stack_name', s)
        self.assertEqual(self.stack.name, s['stack_name'])
        self.assertIn('stack_status', s)
        self.assertIn('stack_status_reason', s)
        self.assertIn('description', s)
        self.assertIn('WordPress', s['description'])
        self.assertIn('parameters', s)

    def test_list_resource_types(self):
        resources = self.eng.list_resource_types(self.ctx)
        self.assertIsInstance(resources, list)
        self.assertIn('AWS::EC2::Instance', resources)
        self.assertIn('AWS::RDS::DBInstance', resources)

    def test_list_resource_types_deprecated(self):
        resources = self.eng.list_resource_types(self.ctx, "DEPRECATED")
        self.assertEqual(set(['OS::Neutron::RouterGateway',
                              'OS::Heat::CWLiteAlarm',
                              'OS::Heat::HARestarter',
                              'OS::Heat::SoftwareDeployments']),
                         set(resources))

    def test_list_resource_types_supported(self):
        resources = self.eng.list_resource_types(self.ctx, "SUPPORTED")
        self.assertNotIn(['OS::Neutron::RouterGateway'], resources)
        self.assertIn('AWS::EC2::Instance', resources)

    def test_resource_schema(self):
        type_name = 'ResourceWithPropsType'
        expected = {
            'resource_type': type_name,
            'properties': {
                'Foo': {
                    'type': 'string',
                    'required': False,
                    'update_allowed': False,
                    'immutable': False,
                },
                'FooInt': {
                    'type': 'integer',
                    'required': False,
                    'update_allowed': False,
                    'immutable': False,
                },
            },
            'attributes': {
                'foo': {'description': 'A generic attribute'},
                'Foo': {'description': 'Another generic attribute'},
            },
        }

        schema = self.eng.resource_schema(self.ctx, type_name=type_name)
        self.assertEqual(expected, schema)

    def test_resource_schema_with_attr_type(self):
        res._register_class('ResourceWithAttributeType',
                            generic_rsrc.ResourceWithAttributeType)

        type_name = 'ResourceWithAttributeType'
        expected = {
            'resource_type': type_name,
            'properties': {},
            'attributes': {
                'attr1': {'description': 'A generic attribute',
                          'type': 'string'},
                'attr2': {'description': 'Another generic attribute',
                          'type': 'map'},
            },
        }
        schema = self.eng.resource_schema(self.ctx, type_name=type_name)
        self.assertEqual(expected, schema)

    def _no_template_file(self, function):
        env = environment.Environment()
        info = environment.ResourceInfo(env.registry,
                                        ['ResourceWithWrongRefOnFile'],
                                        'not_existing.yaml')
        mock_iterable = mock.MagicMock(return_value=iter([info]))
        with mock.patch('heat.engine.environment.ResourceRegistry.iterable_by',
                        new=mock_iterable):
            ex = self.assertRaises(exception.TemplateNotFound,
                                   function,
                                   self.ctx,
                                   type_name='ResourceWithWrongRefOnFile')
            msg = 'Could not fetch remote template "not_existing.yaml"'
            self.assertIn(msg, six.text_type(ex))

    def test_resource_schema_no_template_file(self):
        self._no_template_file(self.eng.resource_schema)

    def test_generate_template_no_template_file(self):
        self._no_template_file(self.eng.generate_template)

    def test_resource_schema_nonexist(self):
        ex = self.assertRaises(exception.ResourceTypeNotFound,
                               self.eng.resource_schema,
                               self.ctx, type_name='Bogus')
        msg = 'The Resource Type (Bogus) could not be found.'
        self.assertEqual(msg, six.text_type(ex))

    def _test_describe_stack_resource(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)
        self.m.ReplayAll()

        r = self.eng.describe_stack_resource(self.ctx, self.stack.identifier(),
                                             'WebServer', with_attr=None)

        self.assertIn('resource_identity', r)
        self.assertIn('description', r)
        self.assertIn('updated_time', r)
        self.assertIn('stack_identity', r)
        self.assertIsNotNone(r['stack_identity'])
        self.assertIn('stack_name', r)
        self.assertEqual(self.stack.name, r['stack_name'])
        self.assertIn('metadata', r)
        self.assertIn('resource_status', r)
        self.assertIn('resource_status_reason', r)
        self.assertIn('resource_type', r)
        self.assertIn('physical_resource_id', r)
        self.assertIn('resource_name', r)
        self.assertIn('attributes', r)
        self.assertEqual('WebServer', r['resource_name'])

        self.m.VerifyAll()

    @tools.stack_context('service_stack_resource_describe__test_stack')
    def test_stack_resource_describe(self):
        self._test_describe_stack_resource()

    def test_stack_resource_describe_nonexist_stack(self):
        non_exist_identifier = identifier.HeatIdentifier(
            self.ctx.tenant_id,
            'wibble',
            '18d06e2e-44d3-4bef-9fbf-52480d604b02')

        stack_not_found_exc = exception.StackNotFound(stack_name='test')
        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        service.EngineService._get_stack(
            self.ctx, non_exist_identifier).AndRaise(stack_not_found_exc)
        self.m.ReplayAll()

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.describe_stack_resource,
                               self.ctx, non_exist_identifier, 'WebServer')
        self.assertEqual(exception.StackNotFound, ex.exc_info[0])

        self.m.VerifyAll()

    @tools.stack_context('service_resource_describe_nonexist_test_stack')
    def test_stack_resource_describe_nonexist_resource(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)

        self.m.ReplayAll()
        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.describe_stack_resource,
                               self.ctx, self.stack.identifier(), 'foo')
        self.assertEqual(exception.ResourceNotFound, ex.exc_info[0])

        self.m.VerifyAll()

    @tools.stack_context('service_resource_describe_noncreated_test_stack',
                         create_res=False)
    def test_stack_resource_describe_noncreated_resource(self):
        self._test_describe_stack_resource()

    @tools.stack_context('service_resource_describe_user_deny_test_stack')
    def test_stack_resource_describe_stack_user_deny(self):
        self.ctx.roles = [cfg.CONF.heat_stack_user_role]
        self.m.StubOutWithMock(service.EngineService, '_authorize_stack_user')
        service.EngineService._authorize_stack_user(self.ctx, mox.IgnoreArg(),
                                                    'foo').AndReturn(False)
        self.m.ReplayAll()

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.describe_stack_resource,
                               self.ctx, self.stack.identifier(), 'foo')
        self.assertEqual(exception.Forbidden, ex.exc_info[0])

        self.m.VerifyAll()

    @tools.stack_context('service_resources_describe_test_stack')
    def test_stack_resources_describe(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)
        self.m.ReplayAll()

        resources = self.eng.describe_stack_resources(self.ctx,
                                                      self.stack.identifier(),
                                                      'WebServer')

        self.assertEqual(1, len(resources))
        r = resources[0]
        self.assertIn('resource_identity', r)
        self.assertIn('description', r)
        self.assertIn('updated_time', r)
        self.assertIn('stack_identity', r)
        self.assertIsNotNone(r['stack_identity'])
        self.assertIn('stack_name', r)
        self.assertEqual(self.stack.name, r['stack_name'])
        self.assertIn('resource_status', r)
        self.assertIn('resource_status_reason', r)
        self.assertIn('resource_type', r)
        self.assertIn('physical_resource_id', r)
        self.assertIn('resource_name', r)
        self.assertEqual('WebServer', r['resource_name'])

        self.m.VerifyAll()

    @tools.stack_context('service_resources_describe_no_filter_test_stack')
    def test_stack_resources_describe_no_filter(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)
        self.m.ReplayAll()

        resources = self.eng.describe_stack_resources(self.ctx,
                                                      self.stack.identifier(),
                                                      None)

        self.assertEqual(1, len(resources))
        r = resources[0]
        self.assertIn('resource_name', r)
        self.assertEqual('WebServer', r['resource_name'])

        self.m.VerifyAll()

    def test_stack_resources_describe_bad_lookup(self):
        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        service.EngineService._get_stack(
            self.ctx, None).AndRaise(TypeError)
        self.m.ReplayAll()

        self.assertRaises(TypeError,
                          self.eng.describe_stack_resources,
                          self.ctx, None, 'WebServer')
        self.m.VerifyAll()

    def test_stack_resources_describe_nonexist_stack(self):
        non_exist_identifier = identifier.HeatIdentifier(
            self.ctx.tenant_id, 'wibble',
            '18d06e2e-44d3-4bef-9fbf-52480d604b02')

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.describe_stack_resources,
                               self.ctx, non_exist_identifier, 'WebServer')
        self.assertEqual(exception.StackNotFound, ex.exc_info[0])

    @tools.stack_context('find_phys_res_stack')
    def test_find_physical_resource(self):
        resources = self.eng.describe_stack_resources(self.ctx,
                                                      self.stack.identifier(),
                                                      None)
        phys_id = resources[0]['physical_resource_id']

        result = self.eng.find_physical_resource(self.ctx, phys_id)
        self.assertIsInstance(result, dict)
        resource_identity = identifier.ResourceIdentifier(**result)
        self.assertEqual(self.stack.identifier(), resource_identity.stack())
        self.assertEqual('WebServer', resource_identity.resource_name)

    def test_find_physical_resource_nonexist(self):
        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.find_physical_resource,
                               self.ctx, 'foo')
        self.assertEqual(exception.PhysicalResourceNotFound, ex.exc_info[0])

    @tools.stack_context('service_resources_list_test_stack')
    def test_stack_resources_list(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)
        self.m.ReplayAll()

        resources = self.eng.list_stack_resources(self.ctx,
                                                  self.stack.identifier())

        self.assertEqual(1, len(resources))
        r = resources[0]
        self.assertIn('resource_identity', r)
        self.assertIn('updated_time', r)
        self.assertIn('physical_resource_id', r)
        self.assertIn('resource_name', r)
        self.assertEqual('WebServer', r['resource_name'])
        self.assertIn('resource_status', r)
        self.assertIn('resource_status_reason', r)
        self.assertIn('resource_type', r)

        self.m.VerifyAll()

    @mock.patch.object(parser.Stack, 'load')
    @tools.stack_context('service_resources_list_test_stack_with_depth')
    def test_stack_resources_list_with_depth(self, mock_load):
        mock_load.return_value = self.stack
        resources = six.itervalues(self.stack)
        self.stack.iter_resources = mock.Mock(return_value=resources)
        resources = self.eng.list_stack_resources(self.ctx,
                                                  self.stack.identifier(),
                                                  2)
        self.stack.iter_resources.assert_called_once_with(2)

    @mock.patch.object(parser.Stack, 'load')
    @tools.stack_context('service_resources_list_test_stack_with_max_depth')
    def test_stack_resources_list_with_max_depth(self, mock_load):
        mock_load.return_value = self.stack
        resources = six.itervalues(self.stack)
        self.stack.iter_resources = mock.Mock(return_value=resources)
        resources = self.eng.list_stack_resources(self.ctx,
                                                  self.stack.identifier(),
                                                  99)
        max_depth = cfg.CONF.max_nested_stack_depth
        self.stack.iter_resources.assert_called_once_with(max_depth)

    @mock.patch.object(parser.Stack, 'load')
    def test_stack_resources_list_deleted_stack(self, mock_load):
        stack = tools.setup_stack('resource_list_deleted_stack', self.ctx)
        stack_id = stack.identifier()
        mock_load.return_value = stack
        tools.clean_up_stack(stack)
        resources = self.eng.list_stack_resources(self.ctx, stack_id)
        self.assertEqual(1, len(resources))

        res = resources[0]
        self.assertEqual('DELETE', res['resource_action'])
        self.assertEqual('COMPLETE', res['resource_status'])

    def test_stack_resources_list_nonexist_stack(self):
        non_exist_identifier = identifier.HeatIdentifier(
            self.ctx.tenant_id, 'wibble',
            '18d06e2e-44d3-4bef-9fbf-52480d604b02')

        stack_not_found_exc = exception.StackNotFound(stack_name='test')
        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        service.EngineService._get_stack(
            self.ctx, non_exist_identifier, show_deleted=True
        ).AndRaise(stack_not_found_exc)
        self.m.ReplayAll()

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.list_stack_resources,
                               self.ctx, non_exist_identifier)
        self.assertEqual(exception.StackNotFound, ex.exc_info[0])

        self.m.VerifyAll()

    def test_signal_reception_async(self):
        stack_name = 'signal_reception_async'
        stack = tools.get_stack(stack_name, self.ctx, policy_template)
        self.stack = stack
        tools.setup_keystone_mocks(self.m, stack)
        self.m.ReplayAll()
        stack.store()
        stack.create()
        test_data = {'food': 'yum'}

        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        s = stack_object.Stack.get_by_id(self.ctx, self.stack.id)
        service.EngineService._get_stack(self.ctx,
                                         self.stack.identifier()).AndReturn(s)

        # Mock out the aync work of thread starting
        self.eng.thread_group_mgr.groups[stack.id] = tools.DummyThreadGroup()
        self.m.StubOutWithMock(self.eng.thread_group_mgr, 'start')
        self.eng.thread_group_mgr.start(stack.id,
                                        mox.IgnoreArg(),
                                        mox.IgnoreArg(),
                                        mox.IgnoreArg(),
                                        mox.IgnoreArg()).AndReturn(None)

        self.m.ReplayAll()

        self.eng.resource_signal(self.ctx,
                                 dict(self.stack.identifier()),
                                 'WebServerScaleDownPolicy',
                                 test_data)

        self.m.VerifyAll()

        self.stack.delete()

    def test_signal_reception_sync(self):
        stack_name = 'signal_reception_sync'
        stack = tools.get_stack(stack_name, self.ctx, policy_template)
        self.stack = stack
        tools.setup_keystone_mocks(self.m, stack)
        self.m.ReplayAll()
        stack.store()
        stack.create()
        test_data = {'food': 'yum'}

        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        s = stack_object.Stack.get_by_id(self.ctx, self.stack.id)
        service.EngineService._get_stack(self.ctx,
                                         self.stack.identifier()).AndReturn(s)

        self.m.StubOutWithMock(res.Resource, 'signal')
        res.Resource.signal(mox.IgnoreArg()).AndReturn(None)
        self.m.ReplayAll()

        self.eng.resource_signal(self.ctx,
                                 dict(self.stack.identifier()),
                                 'WebServerScaleDownPolicy',
                                 test_data,
                                 sync_call=True)

        self.m.VerifyAll()
        self.stack.delete()

    def test_signal_reception_no_resource(self):
        stack_name = 'signal_reception_no_resource'
        stack = tools.get_stack(stack_name, self.ctx, policy_template)
        tools.setup_keystone_mocks(self.m, stack)
        self.stack = stack
        self.m.ReplayAll()
        stack.store()
        stack.create()
        test_data = {'food': 'yum'}

        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        s = stack_object.Stack.get_by_id(self.ctx, self.stack.id)
        service.EngineService._get_stack(self.ctx,
                                         self.stack.identifier()).AndReturn(s)
        self.m.ReplayAll()

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.resource_signal, self.ctx,
                               dict(self.stack.identifier()),
                               'resource_does_not_exist',
                               test_data)
        self.assertEqual(exception.ResourceNotFound, ex.exc_info[0])
        self.m.VerifyAll()
        self.stack.delete()

    def test_signal_reception_unavailable_resource(self):
        stack_name = 'signal_reception_unavailable_resource'
        stack = tools.get_stack(stack_name, self.ctx, policy_template)
        stack.store()
        self.stack = stack
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(
            self.ctx, stack=mox.IgnoreArg(),
            use_stored_context=mox.IgnoreArg()
        ).AndReturn(self.stack)
        self.m.ReplayAll()

        test_data = {'food': 'yum'}
        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        s = stack_object.Stack.get_by_id(self.ctx, self.stack.id)
        service.EngineService._get_stack(self.ctx,
                                         self.stack.identifier()).AndReturn(s)
        self.m.ReplayAll()

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.resource_signal, self.ctx,
                               dict(self.stack.identifier()),
                               'WebServerScaleDownPolicy',
                               test_data)
        self.assertEqual(exception.ResourceNotAvailable, ex.exc_info[0])
        self.m.VerifyAll()
        self.stack.delete()

    def test_signal_returns_metadata(self):
        stack = tools.get_stack('signal_reception', self.ctx, policy_template)
        self.stack = stack
        tools.setup_keystone_mocks(self.m, stack)
        self.m.ReplayAll()
        stack.store()
        stack.create()
        test_metadata = {'food': 'yum'}
        rsrc = stack['WebServerScaleDownPolicy']
        rsrc.metadata_set(test_metadata)

        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        s = stack_object.Stack.get_by_id(self.ctx, self.stack.id)
        service.EngineService._get_stack(self.ctx,
                                         self.stack.identifier()).AndReturn(s)

        self.m.StubOutWithMock(res.Resource, 'signal')
        res.Resource.signal(mox.IgnoreArg()).AndReturn(None)
        self.m.ReplayAll()

        md = self.eng.resource_signal(self.ctx,
                                      dict(self.stack.identifier()),
                                      'WebServerScaleDownPolicy', None)

        self.eng.thread_group_mgr.groups[stack.id].wait()
        self.assertIsNone(md)
        self.m.VerifyAll()

    @tools.stack_context('service_metadata_test_stack')
    def test_metadata(self):
        test_metadata = {'foo': 'bar', 'baz': 'quux', 'blarg': 'wibble'}
        pre_update_meta = self.stack['WebServer'].metadata_get()

        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        s = stack_object.Stack.get_by_id(self.ctx, self.stack.id)
        service.EngineService._get_stack(self.ctx,
                                         self.stack.identifier()).AndReturn(s)
        self.m.StubOutWithMock(instances.Instance, 'metadata_update')
        instances.Instance.metadata_update(new_metadata=test_metadata)
        self.m.ReplayAll()

        result = self.eng.metadata_update(self.ctx,
                                          dict(self.stack.identifier()),
                                          'WebServer', test_metadata)
        # metadata_update is a no-op for all resources except
        # WaitConditionHandle so we don't expect this to have changed
        self.assertEqual(pre_update_meta, result)

        self.m.VerifyAll()

    def test_metadata_err_stack(self):
        non_exist_identifier = identifier.HeatIdentifier(
            self.ctx.tenant_id, 'wibble',
            '18d06e2e-44d3-4bef-9fbf-52480d604b02')

        stack_not_found_exc = exception.StackNotFound(stack_name='test')
        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        service.EngineService._get_stack(
            self.ctx, non_exist_identifier).AndRaise(stack_not_found_exc)
        self.m.ReplayAll()

        test_metadata = {'foo': 'bar', 'baz': 'quux', 'blarg': 'wibble'}
        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.metadata_update,
                               self.ctx, non_exist_identifier,
                               'WebServer', test_metadata)
        self.assertEqual(exception.StackNotFound, ex.exc_info[0])
        self.m.VerifyAll()

    @tools.stack_context('service_metadata_err_resource_test_stack', False)
    def test_metadata_err_resource(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)
        self.m.ReplayAll()

        test_metadata = {'foo': 'bar', 'baz': 'quux', 'blarg': 'wibble'}
        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.metadata_update,
                               self.ctx, dict(self.stack.identifier()),
                               'NooServer', test_metadata)
        self.assertEqual(exception.ResourceNotFound, ex.exc_info[0])

        self.m.VerifyAll()

    @tools.stack_context('service_show_watch_test_stack', False)
    def test_show_watch(self):
        # Insert two dummy watch rules into the DB
        rule = {u'EvaluationPeriods': u'1',
                u'AlarmActions': [u'WebServerRestartPolicy'],
                u'AlarmDescription': u'Restart the WikiDatabase',
                u'Namespace': u'system/linux',
                u'Period': u'300',
                u'ComparisonOperator': u'GreaterThanThreshold',
                u'Statistic': u'SampleCount',
                u'Threshold': u'2',
                u'MetricName': u'ServiceFailure'}
        self.wr = []
        self.wr.append(watchrule.WatchRule(context=self.ctx,
                                           watch_name='show_watch_1',
                                           rule=rule,
                                           watch_data=[],
                                           stack_id=self.stack.id,
                                           state='NORMAL'))
        self.wr[0].store()

        self.wr.append(watchrule.WatchRule(context=self.ctx,
                                           watch_name='show_watch_2',
                                           rule=rule,
                                           watch_data=[],
                                           stack_id=self.stack.id,
                                           state='NORMAL'))
        self.wr[1].store()

        # watch_name=None should return all watches
        result = self.eng.show_watch(self.ctx, watch_name=None)
        result_names = [r.get('name') for r in result]
        self.assertIn('show_watch_1', result_names)
        self.assertIn('show_watch_2', result_names)

        result = self.eng.show_watch(self.ctx, watch_name="show_watch_1")
        self.assertEqual(1, len(result))
        self.assertIn('name', result[0])
        self.assertEqual('show_watch_1', result[0]['name'])

        result = self.eng.show_watch(self.ctx, watch_name="show_watch_2")
        self.assertEqual(1, len(result))
        self.assertIn('name', result[0])
        self.assertEqual('show_watch_2', result[0]['name'])

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.show_watch,
                               self.ctx, watch_name="nonexistent")
        self.assertEqual(exception.WatchRuleNotFound, ex.exc_info[0])

        # Check the response has all keys defined in the engine API
        for key in rpc_api.WATCH_KEYS:
            self.assertIn(key, result[0])

    @tools.stack_context('service_show_watch_metric_test_stack', False)
    def test_show_watch_metric(self):
        # Insert dummy watch rule into the DB
        rule = {u'EvaluationPeriods': u'1',
                u'AlarmActions': [u'WebServerRestartPolicy'],
                u'AlarmDescription': u'Restart the WikiDatabase',
                u'Namespace': u'system/linux',
                u'Period': u'300',
                u'ComparisonOperator': u'GreaterThanThreshold',
                u'Statistic': u'SampleCount',
                u'Threshold': u'2',
                u'MetricName': u'ServiceFailure'}
        self.wr = watchrule.WatchRule(context=self.ctx,
                                      watch_name='show_watch_metric_1',
                                      rule=rule,
                                      watch_data=[],
                                      stack_id=self.stack.id,
                                      state='NORMAL')
        self.wr.store()

        # And add a metric datapoint
        watch = watch_rule_object.WatchRule.get_by_name(self.ctx,
                                                        'show_watch_metric_1')
        self.assertIsNotNone(watch)
        values = {'watch_rule_id': watch.id,
                  'data': {u'Namespace': u'system/linux',
                           u'ServiceFailure': {
                               u'Units': u'Counter', u'Value': 1}}}
        watch_data_object.WatchData.create(self.ctx, values)

        # Check there is one result returned
        result = self.eng.show_watch_metric(self.ctx,
                                            metric_namespace=None,
                                            metric_name=None)
        self.assertEqual(1, len(result))

        # Create another metric datapoint and check we get two
        watch_data_object.WatchData.create(self.ctx, values)
        result = self.eng.show_watch_metric(self.ctx,
                                            metric_namespace=None,
                                            metric_name=None)
        self.assertEqual(2, len(result))

        # Check the response has all keys defined in the engine API
        for key in rpc_api.WATCH_DATA_KEYS:
            self.assertIn(key, result[0])

    @tools.stack_context('service_show_watch_state_test_stack')
    def test_set_watch_state(self):
        # Insert dummy watch rule into the DB
        rule = {u'EvaluationPeriods': u'1',
                u'AlarmActions': [u'WebServerRestartPolicy'],
                u'AlarmDescription': u'Restart the WikiDatabase',
                u'Namespace': u'system/linux',
                u'Period': u'300',
                u'ComparisonOperator': u'GreaterThanThreshold',
                u'Statistic': u'SampleCount',
                u'Threshold': u'2',
                u'MetricName': u'ServiceFailure'}
        self.wr = watchrule.WatchRule(context=self.ctx,
                                      watch_name='OverrideAlarm',
                                      rule=rule,
                                      watch_data=[],
                                      stack_id=self.stack.id,
                                      state='NORMAL')
        self.wr.store()

        class DummyAction(object):
            def signal(self):
                return "dummyfoo"

        dummy_action = DummyAction()
        self.m.StubOutWithMock(parser.Stack, 'resource_by_refid')
        parser.Stack.resource_by_refid(
            'WebServerRestartPolicy').AndReturn(dummy_action)

        # Replace the real stack threadgroup with a dummy one, so we can
        # check the function returned on ALARM is correctly scheduled
        dtg = tools.DummyThreadGroup()
        self.eng.thread_group_mgr.groups[self.stack.id] = dtg

        self.m.ReplayAll()

        state = watchrule.WatchRule.NODATA
        result = self.eng.set_watch_state(self.ctx,
                                          watch_name="OverrideAlarm",
                                          state=state)
        self.assertEqual(state, result[rpc_api.WATCH_STATE_VALUE])
        self.assertEqual(
            [], self.eng.thread_group_mgr.groups[self.stack.id].threads)

        state = watchrule.WatchRule.NORMAL
        result = self.eng.set_watch_state(self.ctx,
                                          watch_name="OverrideAlarm",
                                          state=state)
        self.assertEqual(state, result[rpc_api.WATCH_STATE_VALUE])
        self.assertEqual(
            [], self.eng.thread_group_mgr.groups[self.stack.id].threads)

        state = watchrule.WatchRule.ALARM
        result = self.eng.set_watch_state(self.ctx,
                                          watch_name="OverrideAlarm",
                                          state=state)
        self.assertEqual(state, result[rpc_api.WATCH_STATE_VALUE])
        self.assertEqual(
            [dummy_action.signal],
            self.eng.thread_group_mgr.groups[self.stack.id].threads)

        self.m.VerifyAll()

    @tools.stack_context('service_show_watch_state_badstate_test_stack')
    def test_set_watch_state_badstate(self):
        # Insert dummy watch rule into the DB
        rule = {u'EvaluationPeriods': u'1',
                u'AlarmActions': [u'WebServerRestartPolicy'],
                u'AlarmDescription': u'Restart the WikiDatabase',
                u'Namespace': u'system/linux',
                u'Period': u'300',
                u'ComparisonOperator': u'GreaterThanThreshold',
                u'Statistic': u'SampleCount',
                u'Threshold': u'2',
                u'MetricName': u'ServiceFailure'}
        self.wr = watchrule.WatchRule(context=self.ctx,
                                      watch_name='OverrideAlarm2',
                                      rule=rule,
                                      watch_data=[],
                                      stack_id=self.stack.id,
                                      state='NORMAL')
        self.wr.store()

        self.m.StubOutWithMock(watchrule.WatchRule, 'set_watch_state')
        for state in ["HGJHGJHG", "1234", "!\*(&%"]:
            watchrule.WatchRule.set_watch_state(
                state).InAnyOrder().AndRaise(ValueError)
        self.m.ReplayAll()

        for state in ["HGJHGJHG", "1234", "!\*(&%"]:
            self.assertRaises(ValueError,
                              self.eng.set_watch_state,
                              self.ctx, watch_name="OverrideAlarm2",
                              state=state)

        self.m.VerifyAll()

    def test_set_watch_state_noexist(self):
        state = watchrule.WatchRule.ALARM   # State valid

        self.m.StubOutWithMock(watchrule.WatchRule, 'load')
        watchrule.WatchRule.load(
            self.ctx, "nonexistent"
        ).AndRaise(exception.WatchRuleNotFound(watch_name='test'))
        self.m.ReplayAll()

        ex = self.assertRaises(dispatcher.ExpectedException,
                               self.eng.set_watch_state,
                               self.ctx, watch_name="nonexistent",
                               state=state)
        self.assertEqual(exception.WatchRuleNotFound, ex.exc_info[0])
        self.m.VerifyAll()

    def test_stack_list_all_empty(self):
        sl = self.eng.list_stacks(self.ctx)

        self.assertEqual(0, len(sl))

    def test_stack_describe_all_empty(self):
        sl = self.eng.show_stack(self.ctx, None)

        self.assertEqual(0, len(sl))

    def test_lazy_load_resources(self):
        stack_name = 'lazy_load_test'
        res._register_class('GenericResourceType',
                            generic_rsrc.GenericResource)

        lazy_load_template = {
            'HeatTemplateFormatVersion': '2012-12-12',
            'Resources': {
                'foo': {'Type': 'GenericResourceType'},
                'bar': {
                    'Type': 'ResourceWithPropsType',
                    'Properties': {
                        'Foo': {'Ref': 'foo'},
                    }
                }
            }
        }
        templ = templatem.Template(lazy_load_template)
        stack = parser.Stack(self.ctx, stack_name, templ)

        self.assertIsNone(stack._resources)
        self.assertIsNone(stack._dependencies)

        resources = stack.resources
        self.assertIsInstance(resources, dict)
        self.assertEqual(2, len(resources))
        self.assertIsInstance(resources.get('foo'),
                              generic_rsrc.GenericResource)
        self.assertIsInstance(resources.get('bar'),
                              generic_rsrc.ResourceWithProps)

        stack_dependencies = stack.dependencies
        self.assertIsInstance(stack_dependencies, dependencies.Dependencies)
        self.assertEqual(2, len(stack_dependencies.graph()))

    def _preview_stack(self):
        res._register_class('GenericResource1', generic_rsrc.GenericResource)
        res._register_class('GenericResource2', generic_rsrc.GenericResource)

        args = {}
        params = {}
        files = None
        stack_name = 'SampleStack'
        tpl = {'HeatTemplateFormatVersion': '2012-12-12',
               'Description': 'Lorem ipsum.',
               'Resources': {
                   'SampleResource1': {'Type': 'GenericResource1'},
                   'SampleResource2': {'Type': 'GenericResource2'}}}

        return self.eng.preview_stack(self.ctx, stack_name, tpl,
                                      params, files, args)

    def test_preview_stack_returns_a_stack(self):
        stack = self._preview_stack()
        expected_identity = {'path': '',
                             'stack_id': 'None',
                             'stack_name': 'SampleStack',
                             'tenant': 'stack_service_test_tenant'}
        self.assertEqual(expected_identity, stack['stack_identity'])
        self.assertEqual('SampleStack', stack['stack_name'])
        self.assertEqual('Lorem ipsum.', stack['description'])

    def test_preview_stack_returns_list_of_resources_in_stack(self):
        stack = self._preview_stack()
        self.assertIsInstance(stack['resources'], list)
        self.assertEqual(2, len(stack['resources']))

        resource_types = (r['resource_type'] for r in stack['resources'])
        self.assertIn('GenericResource1', resource_types)
        self.assertIn('GenericResource2', resource_types)

        resource_names = (r['resource_name'] for r in stack['resources'])
        self.assertIn('SampleResource1', resource_names)
        self.assertIn('SampleResource2', resource_names)

    def test_preview_stack_validates_new_stack(self):
        exc = exception.StackExists(stack_name='Validation Failed')
        self.eng._validate_new_stack = mock.Mock(side_effect=exc)
        ex = self.assertRaises(dispatcher.ExpectedException,
                               self._preview_stack)
        self.assertEqual(exception.StackExists, ex.exc_info[0])

    @mock.patch.object(service.api, 'format_stack_preview', new=mock.Mock())
    @mock.patch.object(service.parser, 'Stack')
    def test_preview_stack_checks_stack_validity(self, mock_parser):
        exc = exception.StackValidationFailed(message='Validation Failed')
        mock_parsed_stack = mock.Mock()
        mock_parsed_stack.validate.side_effect = exc
        mock_parser.return_value = mock_parsed_stack
        ex = self.assertRaises(dispatcher.ExpectedException,
                               self._preview_stack)
        self.assertEqual(exception.StackValidationFailed, ex.exc_info[0])

    @mock.patch.object(stack_object.Stack, 'get_by_name')
    def test_validate_new_stack_checks_existing_stack(self, mock_stack_get):
        mock_stack_get.return_value = 'existing_db_stack'
        tmpl = templatem.Template(
            {'HeatTemplateFormatVersion': '2012-12-12'})
        self.assertRaises(exception.StackExists, self.eng._validate_new_stack,
                          self.ctx, 'test_existing_stack', tmpl)

    @mock.patch.object(stack_object.Stack, 'count_all')
    def test_validate_new_stack_checks_stack_limit(self, mock_db_count):
        cfg.CONF.set_override('max_stacks_per_tenant', 99)
        mock_db_count.return_value = 99
        template = templatem.Template(
            {'HeatTemplateFormatVersion': '2012-12-12'})
        self.assertRaises(exception.RequestLimitExceeded,
                          self.eng._validate_new_stack,
                          self.ctx, 'test_existing_stack', template)

    def test_validate_new_stack_checks_incorrect_keywords_in_resource(self):
        template = {'heat_template_version': '2013-05-23',
                    'resources': {
                        'Res': {'Type': 'GenericResource1'}}}
        parsed_template = templatem.Template(template)
        ex = self.assertRaises(exception.StackValidationFailed,
                               self.eng._validate_new_stack,
                               self.ctx, 'test_existing_stack',
                               parsed_template)
        msg = (u'u\'"Type" is not a valid keyword '
               'inside a resource definition\'')
        self.assertEqual(msg, six.text_type(ex))

    def test_validate_new_stack_checks_incorrect_sections(self):
        template = {'heat_template_version': '2013-05-23',
                    'unknown_section': {
                        'Res': {'Type': 'GenericResource1'}}}
        parsed_template = templatem.Template(template)
        ex = self.assertRaises(exception.StackValidationFailed,
                               self.eng._validate_new_stack,
                               self.ctx, 'test_existing_stack',
                               parsed_template)
        msg = u'The template section is invalid: unknown_section'
        self.assertEqual(msg, six.text_type(ex))

    def test_validate_new_stack_checks_resource_limit(self):
        cfg.CONF.set_override('max_resources_per_stack', 5)
        template = {'HeatTemplateFormatVersion': '2012-12-12',
                    'Resources': {
                        'Res1': {'Type': 'GenericResource1'},
                        'Res2': {'Type': 'GenericResource1'},
                        'Res3': {'Type': 'GenericResource1'},
                        'Res4': {'Type': 'GenericResource1'},
                        'Res5': {'Type': 'GenericResource1'},
                        'Res6': {'Type': 'GenericResource1'}}}
        parsed_template = templatem.Template(template)
        self.assertRaises(exception.RequestLimitExceeded,
                          self.eng._validate_new_stack,
                          self.ctx, 'test_existing_stack', parsed_template)

    @mock.patch.object(service_objects.Service, 'get_all')
    @mock.patch.object(service_utils, 'format_service')
    def test_service_get_all(self, mock_format_service, mock_get_all):
        mock_get_all.return_value = [mock.Mock()]
        mock_format_service.return_value = mock.Mock()
        self.assertEqual(1, len(self.eng.list_services(self.ctx)))
        self.assertTrue(mock_get_all.called)
        mock_format_service.assert_called_once_with(mock.ANY)

    @mock.patch.object(service_objects.Service, 'create')
    @mock.patch.object(context, 'get_admin_context')
    def test_service_manage_report_start(self,
                                         mock_admin_context,
                                         mock_service_create):
        self.eng.service_id = None
        mock_admin_context.return_value = self.ctx
        srv = dict(id='mock_id')
        mock_service_create.return_value = srv
        self.eng.service_manage_report()
        mock_admin_context.assert_called_once_with()
        mock_service_create.assert_called_once_with(
            self.ctx,
            dict(host=self.eng.host,
                 hostname=self.eng.hostname,
                 binary=self.eng.binary,
                 engine_id=self.eng.engine_id,
                 topic=self.eng.topic,
                 report_interval=cfg.CONF.periodic_interval))

        self.assertEqual(self.eng.service_id, srv['id'])

    @mock.patch.object(service_objects.Service, 'get_all_by_args')
    @mock.patch.object(service_objects.Service, 'delete')
    @mock.patch.object(context, 'get_admin_context')
    def test_service_manage_report_cleanup(self,
                                           mock_admin_context,
                                           mock_service_delete,
                                           mock_get_all):
        mock_admin_context.return_value = self.ctx
        ages_a_go = datetime.datetime.utcnow() - datetime.timedelta(
            seconds=4000)
        mock_get_all.return_value = [{'id': 'foo',
                                      'deleted_at': None,
                                      'updated_at': ages_a_go}]
        self.eng.service_manage_cleanup()
        mock_admin_context.assert_called_once_with()
        mock_get_all.assert_called_once_with(self.ctx,
                                             self.eng.host,
                                             self.eng.binary,
                                             self.eng.hostname)
        mock_service_delete.assert_called_once_with(
            self.ctx, 'foo')

    @mock.patch.object(service_objects.Service, 'update_by_id')
    @mock.patch.object(context, 'get_admin_context')
    def test_service_manage_report_update(
            self,
            mock_admin_context,
            mock_service_update):
        self.eng.service_id = 'mock_id'
        mock_admin_context.return_value = self.ctx
        self.eng.service_manage_report()
        mock_admin_context.assert_called_once_with()
        mock_service_update.assert_called_once_with(
            self.ctx,
            'mock_id',
            dict(deleted_at=None))

    def test_stop_rpc_server(self):
        with mock.patch.object(self.eng,
                               '_rpc_server') as mock_rpc_server:
            self.eng._stop_rpc_server()
            mock_rpc_server.stop.assert_called_once_with()
            mock_rpc_server.wait.assert_called_once_with()

    def _test_engine_service_start(
            self,
            thread_group_class,
            worker_service_class,
            engine_listener_class,
            thread_group_manager_class,
            sample_uuid_method,
            rpc_client_class,
            target_class,
            rpc_server_method):
        self.patchobject(self.eng, 'service_manage_cleanup')
        self.patchobject(self.eng, 'reset_stack_status')
        self.eng.start()

        # engine id
        sample_uuid_method.assert_called_once_with()
        sampe_uuid = sample_uuid_method.return_value
        self.assertEqual(sampe_uuid,
                         self.eng.engine_id,
                         'Failed to generated engine_id')

        # Thread group manager
        thread_group_manager_class.assert_called_once_with()
        thread_group_manager = thread_group_manager_class.return_value
        self.assertEqual(thread_group_manager,
                         self.eng.thread_group_mgr,
                         'Failed to create Thread Group Manager')

        # Engine Listener
        engine_listener_class.assert_called_once_with(
            self.eng.host,
            self.eng.engine_id,
            self.eng.thread_group_mgr
        )
        engine_lister = engine_listener_class.return_value
        engine_lister.start.assert_called_once_with()

        # Worker Service
        if cfg.CONF.convergence_engine:
            worker_service_class.assert_called_once_with(
                host=self.eng.host,
                topic=worker_api.TOPIC,
                engine_id=self.eng.engine_id,
                thread_group_mgr=self.eng.thread_group_mgr
            )
            worker_service = worker_service_class.return_value
            worker_service.start.assert_called_once_with()

        # RPC Target
        target_class.assert_called_once_with(
            version=service.EngineService.RPC_API_VERSION,
            server=self.eng.host,
            topic=self.eng.topic)

        # RPC server
        target = target_class.return_value
        rpc_server_method.assert_called_once_with(target,
                                                  self.eng)
        rpc_server = rpc_server_method.return_value
        self.assertEqual(rpc_server,
                         self.eng._rpc_server,
                         "Failed to create RPC server")

        rpc_server.start.assert_called_once_with()

        # RPC client
        rpc_client = rpc_client_class.return_value
        rpc_client_class.assert_called_once_with(
            version=service.EngineService.RPC_API_VERSION)
        self.assertEqual(rpc_client,
                         self.eng._client,
                         "Failed to create RPC client")

        # Manage Thread group
        thread_group_class.assert_called_once_with()
        manage_thread_group = thread_group_class.return_value
        manage_thread_group.add_timer.assert_called_once_with(
            cfg.CONF.periodic_interval,
            self.eng.service_manage_report
        )

    @mock.patch('heat.engine.service.ThreadGroupManager',
                return_value=mock.Mock())
    @mock.patch.object(stack_object.Stack, 'get_all')
    @mock.patch('heat.engine.stack_lock.StackLock',
                return_value=mock.Mock())
    @mock.patch.object(parser.Stack, 'load')
    @mock.patch.object(context, 'get_admin_context')
    def test_engine_reset_stack_status(
            self,
            mock_admin_context,
            mock_stack_load,
            mock_stacklock,
            mock_get_all,
            mock_thread):
        mock_admin_context.return_value = self.ctx

        db_stack = mock.MagicMock()
        db_stack.id = 'foo'
        db_stack.status = 'IN_PROGRESS'
        db_stack.status_reason = None
        mock_get_all.return_value = [db_stack]

        fake_stack = mock.MagicMock()
        fake_stack.action = 'CREATE'
        fake_stack.id = 'foo'
        fake_stack.status = 'IN_PROGRESS'
        fake_stack.state_set.return_value = None
        mock_stack_load.return_value = fake_stack

        fake_lock = mock.MagicMock()
        fake_lock.get_engine_id.return_value = 'old-engine'
        fake_lock.acquire.return_value = None
        mock_stacklock.return_value = fake_lock

        self.eng.thread_group_mgr = mock_thread

        self.eng.reset_stack_status()

        mock_admin_context.assert_called_once_with()
        filters = {'status': parser.Stack.IN_PROGRESS}
        mock_get_all.assert_called_once_with(self.ctx,
                                             filters=filters,
                                             tenant_safe=False)
        mock_stack_load.assert_call_once_with(self.ctx,
                                              stack=db_stack,
                                              use_stored_context=True)
        mock_thread.start_with_acquired_lock.assert_call_once_with(
            fake_stack, fake_stack.state_set, fake_stack.action,
            parser.Stack.FAILED, 'Engine went down during stack CREATE'
        )

    @mock.patch('heat.common.messaging.get_rpc_server',
                return_value=mock.Mock())
    @mock.patch('oslo_messaging.Target',
                return_value=mock.Mock())
    @mock.patch('heat.common.messaging.get_rpc_client',
                return_value=mock.Mock())
    @mock.patch('heat.engine.stack_lock.StackLock.generate_engine_id',
                return_value='sample-uuid')
    @mock.patch('heat.engine.service.ThreadGroupManager',
                return_value=mock.Mock())
    @mock.patch('heat.engine.service.EngineListener',
                return_value=mock.Mock())
    @mock.patch('heat.openstack.common.threadgroup.ThreadGroup',
                return_value=mock.Mock())
    def test_engine_service_start_in_non_convergence_mode(
            self,
            thread_group_class,
            engine_listener_class,
            thread_group_manager_class,
            sample_uuid_method,
            rpc_client_class,
            target_class,
            rpc_server_method):
        cfg.CONF.set_default('convergence_engine', False)
        self._test_engine_service_start(
            thread_group_class,
            None,
            engine_listener_class,
            thread_group_manager_class,
            sample_uuid_method,
            rpc_client_class,
            target_class,
            rpc_server_method
        )

    @mock.patch('heat.common.messaging.get_rpc_server',
                return_value=mock.Mock())
    @mock.patch('oslo_messaging.Target',
                return_value=mock.Mock())
    @mock.patch('heat.common.messaging.get_rpc_client',
                return_value=mock.Mock())
    @mock.patch('heat.engine.stack_lock.StackLock.generate_engine_id',
                return_value=mock.Mock())
    @mock.patch('heat.engine.service.ThreadGroupManager',
                return_value=mock.Mock())
    @mock.patch('heat.engine.service.EngineListener',
                return_value=mock.Mock())
    @mock.patch('heat.engine.worker.WorkerService',
                return_value=mock.Mock())
    @mock.patch('heat.openstack.common.threadgroup.ThreadGroup',
                return_value=mock.Mock())
    def test_engine_service_start_in_convergence_mode(
            self,
            thread_group_class,
            worker_service_class,
            engine_listener_class,
            thread_group_manager_class,
            sample_uuid_method,
            rpc_client_class,
            target_class,
            rpc_server_method):
        cfg.CONF.set_default('convergence_engine', True)
        self._test_engine_service_start(
            thread_group_class,
            worker_service_class,
            engine_listener_class,
            thread_group_manager_class,
            sample_uuid_method,
            rpc_client_class,
            target_class,
            rpc_server_method
        )

    def _test_engine_service_stop(
            self,
            service_delete_method,
            admin_context_method):
        cfg.CONF.set_default('periodic_interval', 60)
        self.patchobject(self.eng, 'service_manage_cleanup')
        self.patchobject(self.eng, 'reset_stack_status')

        self.eng.start()
        # Add dummy thread group to test thread_group_mgr.stop() is executed?
        dtg1 = tools.DummyThreadGroup()
        dtg2 = tools.DummyThreadGroup()
        self.eng.thread_group_mgr.groups['sample-uuid1'] = dtg1
        self.eng.thread_group_mgr.groups['sample-uuid2'] = dtg2
        self.eng.service_id = 'sample-service-uuid'

        orig_stop = self.eng.thread_group_mgr.stop

        with mock.patch.object(self.eng.thread_group_mgr, 'stop') as stop:
            stop.side_effect = orig_stop

            self.eng.stop()

            # RPC server
            self.eng._stop_rpc_server.assert_called_once_with()

            if cfg.CONF.convergence_engine:
                # WorkerService
                self.eng.worker_service.stop.assert_called_once_with()

            # Wait for all active threads to be finished
            calls = [mock.call('sample-uuid1', True),
                     mock.call('sample-uuid2', True)]
            self.eng.thread_group_mgr.stop.assert_has_calls(
                calls,
                True)

            # # Manage Thread group
            self.eng.manage_thread_grp.stop.assert_called_with(False)

            # Service delete
            admin_context_method.assert_called_once_with()
            ctxt = admin_context_method.return_value
            service_delete_method.assert_called_once_with(
                ctxt,
                self.eng.service_id
            )

    @mock.patch.object(service.EngineService,
                       '_stop_rpc_server')
    @mock.patch.object(worker.WorkerService,
                       'stop')
    @mock.patch.object(threadgroup.ThreadGroup,
                       'stop')
    @mock.patch('heat.common.context.get_admin_context',
                return_value=mock.Mock())
    @mock.patch('heat.objects.service.Service.delete',
                return_value=mock.Mock())
    def test_engine_service_stop_in_convergence_mode(
            self,
            service_delete_method,
            admin_context_method,
            thread_group_stop,
            worker_service_stop,
            rpc_server_stop):
        cfg.CONF.set_default('convergence_engine', True)
        self._test_engine_service_stop(
            service_delete_method,
            admin_context_method
        )

    @mock.patch.object(service.EngineService,
                       '_stop_rpc_server')
    @mock.patch.object(threadgroup.ThreadGroup,
                       'stop')
    @mock.patch('heat.common.context.get_admin_context',
                return_value=mock.Mock())
    @mock.patch('heat.objects.service.Service.delete',
                return_value=mock.Mock())
    def test_engine_service_stop_in_non_convergence_mode(
            self,
            service_delete_method,
            admin_context_method,
            thread_group_stop,
            rpc_server_stop):
        cfg.CONF.set_default('convergence_engine', False)
        self._test_engine_service_stop(
            service_delete_method,
            admin_context_method
        )


class ThreadGroupManagerTest(common.HeatTestCase):
    def setUp(self):
        super(ThreadGroupManagerTest, self).setUp()
        self.f = 'function'
        self.fargs = ('spam', 'ham', 'eggs')
        self.fkwargs = {'foo': 'bar'}
        self.cnxt = 'ctxt'
        self.engine_id = 'engine_id'
        self.stack = mock.Mock()
        self.lock_mock = mock.Mock()
        self.stlock_mock = self.patch('heat.engine.service.stack_lock')
        self.stlock_mock.StackLock.return_value = self.lock_mock
        self.tg_mock = mock.Mock()
        self.thg_mock = self.patch('heat.engine.service.threadgroup')
        self.thg_mock.ThreadGroup.return_value = self.tg_mock
        self.cfg_mock = self.patch('heat.engine.service.cfg')

    def test_tgm_start_with_lock(self):
        thm = service.ThreadGroupManager()
        with self.patchobject(thm, 'start_with_acquired_lock'):
            mock_thread_lock = mock.Mock()
            mock_thread_lock.__enter__ = mock.Mock(return_value=None)
            mock_thread_lock.__exit__ = mock.Mock(return_value=None)
            self.lock_mock.thread_lock.return_value = mock_thread_lock
            thm.start_with_lock(self.cnxt, self.stack, self.engine_id, self.f,
                                *self.fargs, **self.fkwargs)
            self.stlock_mock.StackLock.assert_called_with(self.cnxt,
                                                          self.stack.id,
                                                          self.engine_id)

            thm.start_with_acquired_lock.assert_called_once_with(
                self.stack, self.lock_mock,
                self.f, *self.fargs, **self.fkwargs)

    def test_tgm_start(self):
        stack_id = 'test'

        thm = service.ThreadGroupManager()
        ret = thm.start(stack_id, self.f, *self.fargs, **self.fkwargs)

        self.assertEqual(self.tg_mock, thm.groups['test'])
        self.tg_mock.add_thread.assert_called_with(
            thm._start_with_trace, None,
            self.f, *self.fargs, **self.fkwargs)
        self.assertEqual(ret, self.tg_mock.add_thread())

    def test_tgm_add_timer(self):
        stack_id = 'test'

        thm = service.ThreadGroupManager()
        thm.add_timer(stack_id, self.f, *self.fargs, **self.fkwargs)

        self.assertEqual(self.tg_mock, thm.groups[stack_id])
        self.tg_mock.add_timer.assert_called_with(
            self.cfg_mock.CONF.periodic_interval,
            self.f, *self.fargs, **self.fkwargs)

    def test_tgm_add_event(self):
        stack_id = 'add_events_test'
        e1, e2 = mock.Mock(), mock.Mock()
        thm = service.ThreadGroupManager()
        thm.add_event(stack_id, e1)
        thm.add_event(stack_id, e2)
        self.assertEqual([e1, e2], thm.events[stack_id])

    def test_tgm_remove_event(self):
        stack_id = 'add_events_test'
        e1, e2 = mock.Mock(), mock.Mock()
        thm = service.ThreadGroupManager()
        thm.add_event(stack_id, e1)
        thm.add_event(stack_id, e2)
        thm.remove_event(None, stack_id, e2)
        self.assertEqual([e1], thm.events[stack_id])
        thm.remove_event(None, stack_id, e1)
        self.assertNotIn(stack_id, thm.events)

    def test_tgm_send(self):
        stack_id = 'send_test'
        e1, e2 = mock.MagicMock(), mock.Mock()
        thm = service.ThreadGroupManager()
        thm.add_event(stack_id, e1)
        thm.add_event(stack_id, e2)
        thm.send(stack_id, 'test_message')


class ThreadGroupManagerStopTest(common.HeatTestCase):
    def test_tgm_stop(self):
        stack_id = 'test'
        done = []

        def function():
            while True:
                eventlet.sleep()

        def linked(gt, thread):
            for i in range(10):
                eventlet.sleep()
            done.append(thread)

        thm = service.ThreadGroupManager()
        thm.add_event(stack_id, mock.Mock())
        thread = thm.start(stack_id, function)
        thread.link(linked, thread)

        thm.stop(stack_id)

        self.assertIn(thread, done)
        self.assertNotIn(stack_id, thm.groups)
        self.assertNotIn(stack_id, thm.events)
