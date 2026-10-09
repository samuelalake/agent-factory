import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_factory import task_route as route
from agent_factory.cli import default_config
from agent_factory.config import parse_config
from agent_factory import github_builder as builder
from agent_factory import github_steward as steward
from agent_factory import github_delivery as delivery

REPO = 'owner/repo'
SOURCE = 'a' * 40
CONTROLLER = 'b' * 40
HEAD = 'c' * 40


class TaskRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'config.json'
        self.data = default_config('demo')
        self.data['builder']['base_branch'] = 'main'
        self.data['steward']['trusted_operator_logins'] = ['operator']
        self.data['routing'] = {'repository': REPO, 'allowed_base_branches': ['main', 'release/demo']}
        self.path.write_text(json.dumps(self.data))
        self.receipt = dict(version=1, repository=REPO, issue=7, base_ref='release/demo',
                            source_sha=SOURCE, controller_sha=CONTROLLER)
        self.comments = []
        self.meta = dict(state='open', body=route.encode(self.receipt),
                         user={'login': self.data['builder']['app_login']},
                         head={'sha':HEAD, 'ref':self.data['builder']['branch_prefix']+'7', 'repo':{'full_name':REPO}},
                         base={'ref':'release/demo', 'repo':{'full_name':REPO}})
        self.permission = 'write'
        self.comparison = 'ahead'

    def api(self, path, **kwargs):
        if path == 'graphql':
            node_id = kwargs['payload']['variables']['id']
            comment = next(c for c in self.comments if c.get('node_id') == node_id)
            return {'data':{'node':{
                '__typename':'IssueComment', 'id':node_id, 'fullDatabaseId':str(comment['id']),
                'body':comment['body'], 'lastEditedAt':comment.get('lastEditedAt'),
                'author':{'__typename':'Bot','login':comment['user']['login'].removesuffix('[bot]')},
                'repository':{'nameWithOwner':REPO},'issue':{'number':7}}}}
        if '/comments?' in path: return self.comments
        if '/permission' in path: return {'permission':self.permission}
        if '/git/ref/heads/' in path: return {'object':{'sha':SOURCE}}
        if '/compare/' in path: return {'status':self.comparison}
        if '/pulls/' in path: return self.meta
        raise AssertionError(path)

    def save(self):
        self.comments = [{'id':1, 'node_id':'one', 'user':{'login':self.data['steward']['app_login']},
                          'body':route.encode(self.receipt)},
                         {'id':2,'node_id':'two','user':{'login':self.data['builder']['app_login']},
                          'body':route.HEAD_MARKER+'\n'+json.dumps({'route':self.receipt,'pr':8,'head':HEAD})}]

    def test_explicit_non_main_and_repository_default(self):
        with patch.object(route, 'api', side_effect=self.api):
            selected = route.resolve(self.path, REPO, '7', 'release/demo', 'operator', CONTROLLER)
            self.assertEqual(selected, self.receipt)
            selected = route.resolve(self.path, REPO, '7', '', 'operator', CONTROLLER)
            self.assertEqual(selected['base_ref'], 'main')

    def test_steward_receipt_is_reused_by_builder_without_floating_sha(self):
        self.save()
        with patch.object(route, 'api', side_effect=self.api):
            self.assertEqual(route.resolve(self.path, REPO, '7', '', self.data['steward']['app_login'], CONTROLLER, event_name='issues', event_actor=self.data['steward']['app_login']), self.receipt)
            with self.assertRaisesRegex(ValueError, 'another controller'):
                route.resolve(self.path, REPO, '7', '', 'operator', HEAD)

    def test_rejects_untrusted_selector_repository_branch_and_missing_branch(self):
        with patch.object(route, 'api', side_effect=self.api):
            for repo, ref, actor in [('other/repo','main','operator'), (REPO,'main','stranger'),
                                     (REPO,'not-allowed','operator')]:
                with self.subTest(repo=repo,ref=ref,actor=actor), self.assertRaises(ValueError):
                    route.resolve(self.path, repo, '7', ref, actor, CONTROLLER)
            self.permission = 'read'
            with self.assertRaisesRegex(ValueError, 'write authority'):
                route.resolve(self.path, REPO, '7', 'main', 'operator', CONTROLLER)
        with patch.object(route, 'saved_route', return_value=None), patch.object(route, 'authorize'), \
             patch.object(route, 'api', side_effect=subprocess.CalledProcessError(1,'gh')):
            with self.assertRaises(subprocess.CalledProcessError):
                route.resolve(self.path, REPO, '7', 'main', 'operator', CONTROLLER)

    def test_rejects_revision_expressions_and_output_injection(self):
        for value in ['refs/pull/7/head', 'main~1', '../main', '-main', 'main\nsource_sha=x',
                      'main;echo hi', 'a..b', 'main.lock', 'x/.hidden', SOURCE, 'x//y']:
            with self.subTest(value=value), self.assertRaises(ValueError): route.branch(value)

    def test_unauthenticated_issue_comment_cannot_select_route(self):
        self.save(); self.comments[0]['user']['login']='stranger'
        with patch.object(route, 'api', side_effect=self.api):
            self.assertIsNone(route.saved_route(REPO, '7', self.data))

    def test_review_and_publication_reject_mismatched_identity(self):
        self.save()
        baseline = copy.deepcopy(self.meta)
        with patch.object(route, 'api', side_effect=self.api):
            self.assertEqual(route.validate_pr(REPO,'8',self.path,expected_head=HEAD),self.receipt)
            for field in ['head', 'base', 'repository', 'author', 'body', 'source']:
                self.meta = copy.deepcopy(baseline)
                self.comparison = 'ahead'
                if field=='head': self.meta['head']['sha']=SOURCE
                if field=='base': self.meta['base']['ref']='main'
                if field=='repository': self.meta['head']['repo']['full_name']='fork/repo'
                if field=='author': self.meta['user']['login']='stranger'
                if field=='body': self.meta['body']=route.encode(self.receipt | {'source_sha':HEAD})
                if field=='source': self.comparison='diverged'
                with self.subTest(field=field), self.assertRaises(ValueError):
                    route.validate_pr(REPO,'8',self.path,expected_head=HEAD)

    def test_removing_route_cannot_restore_legacy_bypass(self):
        self.save(); self.meta['body']='No receipt'
        with patch.object(route, 'api', side_effect=self.api), self.assertRaisesRegex(ValueError,'no task route'):
            route.check_pr_route(REPO,'8',self.path,expected_head=HEAD)

    def test_builder_consumes_exact_source_and_legacy_keeps_main(self):
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        for selected, expected in [(self.receipt, SOURCE), (None, 'origin/main')]:
            data = copy.deepcopy(self.data)
            if selected is None: data.pop('routing')
            self.path.write_text(json.dumps(data))
            env={'GH_TOKEN':'test'}
            if selected: env['AGENT_FACTORY_TASK_ROUTE']=json.dumps(selected)
            def stop(args, **kwargs):
                if args[:2]==['git','checkout']:
                    self.assertEqual(args[-1],expected)
                    raise RuntimeError('checkout reached')
                return ''
            with patch.dict(os.environ,env,clear=True), patch.object(route,'record'), \
                 patch.object(builder,'_gh',side_effect=[json.dumps({'state':'OPEN'}),'[]']), \
                 patch.object(builder,'_run',side_effect=stop):
                with self.assertRaisesRegex(RuntimeError,'checkout reached'):
                    builder.run(REPO,'7',self.root,self.path)

    def test_figma_off_config_and_harness_do_not_expose_figma_secret(self):
        self.data['figma']={'enabled':False}
        self.data['builder']['figma_mcp']=False
        config=parse_config(self.data)
        self.assertFalse(route.routed_config(config,self.receipt).figma.enabled)
        with patch.dict(os.environ, {'FIGMA_MCP_OAUTH_BUNDLE':'must-not-pass',
                                    'AGENT_FACTORY_FIGMA_MCP_CONFIG':'must-not-pass'},clear=True):
            env=builder._claude_agent_env(figma_mcp=False)
            self.assertNotIn('FIGMA_MCP_OAUTH_BUNDLE',env)
            self.assertNotIn('AGENT_FACTORY_FIGMA_MCP_CONFIG',env)

    def test_exact_head_receipt_is_revalidated_before_and_after_publication(self):
        body = route.encode(self.receipt) + "\n" + delivery.pending_delivery()
        updated = delivery.replace_delivery(body, delivery.format_delivery('ready','Proof',head=HEAD))
        with patch.object(delivery,'_gh',side_effect=[json.dumps({'headRefOid':HEAD,'body':body}),
                                                    '{}',json.dumps({'headRefOid':HEAD,'body':updated})]) as gh, \
             patch.object(route,'validate_pr',return_value=self.receipt) as verify:
            delivery.publish(REPO,'8',HEAD,'ready','Proof',route_config=self.path)
            self.assertEqual(verify.call_count,3)
            payload=json.loads(gh.call_args_list[1].kwargs['stdin'])
            self.assertEqual(route.decode(payload['body']),self.receipt)
            self.assertEqual(delivery.delivery_head(payload['body']),HEAD)
        with patch.object(delivery,'_gh',return_value=json.dumps({'headRefOid':HEAD,'body':body})) as gh, \
             patch.object(route,'validate_pr',side_effect=ValueError('route mismatch')):
            with self.assertRaisesRegex(ValueError,'route mismatch'):
                delivery.publish(REPO,'8',HEAD,'ready','Proof',route_config=self.path)
            self.assertEqual(gh.call_count,1)  # No privileged write.

    def test_steward_persists_same_route_before_builder_label(self):
        events = []
        def gh(args, **kwargs):
            if args[:2] == ['issue','view']:
                return json.dumps({'state':'OPEN','labels':[{'name':'ready'}]})
            if '/comments' in args[1] and '--paginate' in args: return '[]'
            if '--add-label' in args and 'agent:builder' in args: events.append('dispatch')
            return ''
        with patch.dict(os.environ, {'GH_TOKEN':'test','AGENT_FACTORY_TASK_ROUTE':json.dumps(self.receipt)},clear=True), \
             patch.object(steward,'_gh',side_effect=gh), \
             patch.object(route,'record',side_effect=lambda value: events.append(value)):
            self.assertEqual(steward.run(REPO,'7',self.path),'dispatched')
        self.assertEqual(events,[self.receipt,'dispatch'])

    def test_untrusted_pr_head_cannot_be_executed_with_builder_credentials(self):
        self.comments=[{'id':2,'node_id':'two','user':{'login':self.data['builder']['app_login']},
                       'body':route.HEAD_MARKER+'\n'+json.dumps({'route':self.receipt,'pr':8,'head':HEAD})}]
        with patch.object(route,'api',side_effect=self.api):
            route.authenticate_candidate(self.receipt,8,HEAD,self.data)
            with self.assertRaisesRegex(ValueError,'untrusted or changed'):
                route.authenticate_candidate(self.receipt,8,SOURCE,self.data)
            self.comments[0]['user']['login']='stranger'
            with self.assertRaisesRegex(ValueError,'without its authenticated'):
                route.authenticate_candidate(self.receipt,8,HEAD,self.data)

    def test_routed_builder_cannot_silently_fall_back_without_preflight(self):
        with patch.dict(os.environ, {'GH_TOKEN':'test'},clear=True), \
             self.assertRaisesRegex(ValueError,'preflight task route'):
            builder.run(REPO,'7',self.root,self.path)

    def test_edited_app_receipts_and_unauthorized_reuse_fail_closed(self):
        self.save()
        with patch.object(route,'api',side_effect=self.api):
            with self.assertRaisesRegex(ValueError,'configured operator'):
                route.resolve(self.path,REPO,'7','','unconfigured-writer',CONTROLLER)
            self.comments[0]['lastEditedAt']='2026-10-09T18:00:00Z'
            with self.assertRaisesRegex(ValueError,'edited'):
                route.saved_route(REPO,'7',self.data)
            self.comments[0].pop('lastEditedAt')
            self.comments[1]['lastEditedAt']='2026-10-09T18:00:00Z'
            with self.assertRaisesRegex(ValueError,'edited'):
                route.validate_pr(REPO,'8',self.path,expected_head=HEAD)

    def test_routed_prefix_never_falls_back_when_all_receipts_are_deleted(self):
        self.meta['body']='receipt removed'
        with patch.object(route,'api',side_effect=self.api), self.assertRaisesRegex(ValueError,'no task route'):
            route.check_pr_route(REPO,'8',self.path,expected_head=HEAD)

    def test_missing_candidate_receipt_blocks_reviewer_and_publisher(self):
        self.save(); self.comments=self.comments[:1]
        with patch.object(route,'api',side_effect=self.api), self.assertRaisesRegex(ValueError,'authenticated candidate'):
            route.validate_pr(REPO,'8',self.path,expected_head=HEAD)

    def test_echoed_route_marker_in_status_prose_is_not_an_authority_record(self):
        self.save(); self.comments[0]['body']='Quoted input: '+self.comments[0]['body']
        with patch.object(route,'api',side_effect=self.api), self.assertRaisesRegex(ValueError,'exact envelope'):
            route.saved_route(REPO,'7',self.data)

    def test_legacy_publication_exception_is_exact_head_and_never_execution_authority(self):
        self.data['routing']['legacy_publication_heads']={'8':HEAD}
        self.path.write_text(json.dumps(self.data)); self.meta['body']='legacy'
        with patch.object(route,'api',side_effect=self.api):
            self.assertIsNone(route.check_pr_route(REPO,'8',self.path,expected_head=HEAD))
            self.meta['head']['sha']=SOURCE
            with self.assertRaises(ValueError):
                route.check_pr_route(REPO,'8',self.path,expected_head=SOURCE)
            with self.assertRaises(ValueError):
                route.authenticate_candidate(self.receipt,8,HEAD,self.data)
