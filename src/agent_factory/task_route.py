"""Opt-in task routes. Read policy only from the trusted controller checkout.

A branch selects code, never the workflow, model configuration, or credentials.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote

MARKER = '<!-- agent-factory:task-route -->'
HEAD_MARKER = '<!-- agent-factory:task-head -->'


def api(path, *, payload=None):
    args = ['gh', 'api', path]
    if payload is not None:
        args += ['--method', 'POST', '--input', '-']
    result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def branch(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', value)
            or value.startswith('refs/') or re.fullmatch(r'[0-9a-fA-F]{40}', value)
            or '..' in value or '//' in value or '@{' in value
            or any(part.startswith('.') or part.endswith(('.', '.lock')) for part in value.split('/'))
            or value.endswith('/')):
        raise ValueError('route requires a safe short branch name, not a revision expression')
    return value


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('route requires a full commit SHA')
    return value


def policy(config_path, repo):
    data = json.loads(Path(config_path).read_text())
    route = data.get('routing')
    if not isinstance(route, dict) or route.get('repository') != repo:
        raise ValueError('routing policy must explicitly allow this repository')
    allowed = route.get('allowed_base_branches')
    if not isinstance(allowed, list) or not allowed:
        raise ValueError('routing requires explicit allowed_base_branches')
    for value in allowed:
        branch(value)
    return data, allowed


def decode(body):
    if MARKER not in body:
        return None
    if body.count(MARKER) != 1:
        raise ValueError('ambiguous task route')
    try:
        return json.loads(body.split(MARKER, 1)[1].split('\n```json\n', 1)[1].split('\n```', 1)[0])
    except (ValueError, IndexError) as exc:
        raise ValueError('malformed task route') from exc


def encode(route):
    return MARKER + '\n```json\n' + json.dumps(route, sort_keys=True) + '\n```'


def validate(route, data, repo, issue):
    allowed = data['routing']['allowed_base_branches']
    if (not isinstance(route, dict) or set(route) != {'version', 'repository', 'issue', 'base_ref', 'source_sha', 'controller_sha'}
            or type(route.get('issue')) is not int or route.get('issue', 0) <= 0
            or route.get('version') != 1
            or route.get('repository') != repo or route.get('issue') != int(issue)
            or branch(route.get('base_ref')) not in allowed):
        raise ValueError('task route repository, issue, or allowed base mismatch')
    sha(route.get('source_sha'))
    sha(route.get('controller_sha'))
    return route


def issue_comments(repo, issue):
    # Paginate explicitly; no first-page trust or mutable issue-body authority.
    comments = []
    page = 1
    while True:
        batch = api(f'repos/{repo}/issues/{issue}/comments?per_page=100&page={page}')
        comments.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return comments


def unedited_bot_comment(comment, repo, issue):
    """REST author is not edit provenance: writers can edit other users' comments."""
    node_id = comment.get('node_id')
    if not isinstance(node_id, str) or not node_id:
        raise ValueError('receipt has no immutable comment identity')
    query = """query($id:ID!){node(id:$id){__typename ... on IssueComment {
      id fullDatabaseId body lastEditedAt author {__typename login}
      repository {nameWithOwner} issue {number}
    }}}"""
    result = api('graphql', payload={'query':query, 'variables':{'id':node_id}})
    node = (result.get('data') or {}).get('node') or {}
    author = node.get('author') or {}
    rest_login = (comment.get('user') or {}).get('login', '')
    if (result.get('errors') or node.get('__typename') != 'IssueComment'
            or node.get('id') != node_id or str(node.get('fullDatabaseId')) != str(comment.get('id'))
            or node.get('body') != comment.get('body')
            or 'lastEditedAt' not in node or node['lastEditedAt'] is not None
            or author.get('__typename') != 'Bot'
            or str(author.get('login') or '') + '[bot]' != rest_login
            or node.get('repository', {}).get('nameWithOwner') != repo
            or node.get('issue', {}).get('number') != int(issue)):
        raise ValueError('receipt was edited, changed during verification, or has untrusted provenance')
    return node['body']


def saved_route(repo, issue, data):
    comments = issue_comments(repo, issue)
    authors = {data['steward']['app_login'], data['builder']['app_login']}
    records = [c for c in comments if c.get('user', {}).get('login') in authors
               and MARKER in (c.get('body') or '')]
    if not records:
        return None
    body = unedited_bot_comment(max(records, key=lambda c: c['id']), repo, issue)
    route = validate(decode(body), data, repo, issue)
    if body != encode(route):
        raise ValueError('route receipt is not an exact envelope')
    return route


def authorize(repo, actor, data):
    if actor not in data['steward'].get('trusted_operator_logins', []):
        raise ValueError('only a configured operator may select a task route')
    permission = api(f'repos/{repo}/collaborators/{quote(actor, safe="")}/permission')
    if permission.get('permission') not in {'admin', 'maintain', 'write'}:
        raise ValueError('route selector must retain repository write authority')


def resolve(config_path, repo, issue, selected, actor, controller_sha, *, event_name='', event_actor=''):
    if not re.fullmatch(r'[1-9][0-9]*', str(issue)):
        raise ValueError('positive issue number required')
    data, allowed = policy(config_path, repo)
    saved = saved_route(repo, issue, data)
    if selected or saved is None:
        authorize(repo, actor, data)
        ref = branch(selected or data['builder'].get('base_branch', 'main'))
        if ref not in allowed:
            raise ValueError('base branch is not allowed by controller policy')
        source = api(f'repos/{repo}/git/ref/heads/{quote(ref, safe="")}')['object']['sha']
        route = dict(version=1, repository=repo, issue=int(issue), base_ref=ref,
                     source_sha=sha(source), controller_sha=sha(controller_sha))
    else:
        trusted_app_event = (event_name == 'issues' and event_actor == actor
                             and actor in {data['steward']['app_login'], data['builder']['app_login']})
        if not trusted_app_event:
            authorize(repo, actor, data)
        route = saved
        # A new controller must explicitly reauthorize outstanding task routes.
        if route['controller_sha'] != controller_sha:
            raise ValueError('task route belongs to another controller; operator must select again')
    # Require the named branch to still exist. Never silently float source_sha.
    current = api(f'repos/{repo}/git/ref/heads/{quote(route["base_ref"], safe="")}')['object']['sha']
    comparison = api(f'repos/{repo}/compare/{route["source_sha"]}...{sha(current)}')
    if comparison.get('status') not in {'ahead', 'identical'}:
        raise ValueError('selected source is no longer on the allowed branch')
    return route


def record(route):
    api(f'repos/{route["repository"]}/issues/{route["issue"]}/comments',
        payload={'body': encode(route)})


def record_candidate(route, pr, head):
    receipt = {'route': route, 'pr': int(pr), 'head': sha(head)}
    api(f'repos/{route["repository"]}/issues/{route["issue"]}/comments',
        payload={'body': HEAD_MARKER + '\n' + json.dumps(receipt, sort_keys=True)})


def authenticate_candidate(route, pr, head, data):
    comments = issue_comments(route['repository'], route['issue'])
    records = [c for c in comments if c.get('user', {}).get('login') == data['builder']['app_login']
               and (c.get('body') or '').startswith(HEAD_MARKER + '\n')]
    if not records:
        raise ValueError('Builder will not execute a PR without its authenticated candidate receipt')
    body = unedited_bot_comment(max(records, key=lambda c: c['id']), route['repository'], route['issue'])
    receipt = json.loads(body.split('\n', 1)[1])
    if receipt != {'route': route, 'pr': int(pr), 'head': head}:
        raise ValueError('Builder will not execute an untrusted or changed candidate head')


def routed_config(config, route):
    return replace(config, builder=replace(config.builder, base_branch=route['base_ref']))


def validate_pr(repo, pr, config_path, *, expected_head=None, expected_route=None):
    data, _ = policy(config_path, repo)
    meta = api(f'repos/{repo}/pulls/{pr}')
    route = decode(meta.get('body') or '')
    if route is None:
        raise ValueError('PR has no task route')
    validate(route, data, repo, route.get('issue'))
    saved = saved_route(repo, route['issue'], data)
    if saved != route or (expected_route is not None and route != expected_route):
        raise ValueError('PR route differs from authenticated task route')
    if (meta.get('state') != 'open' or meta['head']['repo']['full_name'] != repo
            or meta['base']['repo']['full_name'] != repo
            or meta['base']['ref'] != route['base_ref']
            or meta['user']['login'] not in {data['builder']['app_login'], *data['steward'].get('trusted_operator_logins', [])}
            or meta['head']['ref'] != data['builder']['branch_prefix'] + str(route['issue'])):
        raise ValueError('PR repository, author, or base/head route mismatch')
    current = api(f'repos/{repo}/git/ref/heads/{quote(route["base_ref"], safe="")}')['object']['sha']
    base_comparison = api(f'repos/{repo}/compare/{route["source_sha"]}...{sha(current)}')
    if base_comparison.get('status') not in {'ahead', 'identical'}:
        raise ValueError('selected source is no longer on the allowed branch')
    head = sha(meta['head']['sha'])
    if expected_head is not None and head != expected_head:
        raise ValueError('PR head does not match exact-head evidence')
    comparison = api(f'repos/{repo}/compare/{route["source_sha"]}...{head}')
    if comparison.get('status') not in {'ahead', 'identical'}:
        raise ValueError('PR does not retain selected source SHA')
    authenticate_candidate(route, pr, head, data)
    return route


def check_pr_route(repo, pr, config_path, *, expected_head):
    """Legacy PRs may lack receipts, but removing a routed PR's receipt cannot opt out."""
    raw = json.loads(Path(config_path).read_text())
    if 'routing' not in raw:
        return None
    data, _ = policy(config_path, repo)
    meta = api(f'repos/{repo}/pulls/{pr}')
    body = meta.get('body') or ''
    prefix = data['builder']['branch_prefix']
    head_ref = meta['head']['ref']
    issue = head_ref[len(prefix):] if head_ref.startswith(prefix) else ''
    legacy_head = data['routing'].get('legacy_publication_heads', {}).get(str(pr))
    if (MARKER not in body and legacy_head == expected_head == meta['head']['sha']
            and meta['head']['repo']['full_name'] == repo and meta['base']['repo']['full_name'] == repo
            and meta['base']['ref'] in data['routing']['allowed_base_branches']):
        # Exact, reviewed compatibility exception only; Builder never takes this path.
        return None
    if MARKER in body or issue.isdigit():
        return validate_pr(repo, pr, config_path, expected_head=expected_head)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--issue', required=True)
    parser.add_argument('--base-ref', default='')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    controller = subprocess.check_output(['git', '-C', str(args.config.parent.parent), 'rev-parse', 'HEAD'], text=True).strip()
    route = resolve(args.config, args.repo, args.issue, args.base_ref,
                    os.environ.get('GITHUB_TRIGGERING_ACTOR', ''), controller,
                    event_name=os.environ.get('GITHUB_EVENT_NAME', ''), event_actor=os.environ.get('GITHUB_ACTOR', ''))
    args.output.write_text(json.dumps(route))
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('route=' + json.dumps(route, separators=(',', ':')) + '\n')
        output.write('source_sha=' + route['source_sha'] + '\n')
        output.write('controller_sha=' + route['controller_sha'] + '\n')


if __name__ == '__main__':
    main()
