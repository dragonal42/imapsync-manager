import asyncio
from pathlib import Path
import shutil
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest
import main
from test_tenants import env, client


@pytest.mark.parametrize('direct', [False, True])
def test_oauth_arguments_accepted_by_embedded_imapsync_parser(env, monkeypatch, direct):
    account = main.load_config()['accounts'][0]
    account.update(authmech1='XOAUTH2', authmech2='XOAUTH2', provider1='google', provider2='google', refresh1='refresh-secret', refresh2='refresh-secret')
    if direct:
        account.update(token1='access-secret', token2='access-secret')
    reply = MagicMock()
    reply.json.return_value = {'access_token': 'access-secret'}
    monkeypatch.setattr(main.requests, 'post', MagicMock(return_value=reply))
    commands = []
    async def launch(*args, **kwargs):
        commands.append(list(args))
        process = MagicMock()
        process.stdout.read = AsyncMock(side_effect=[b'', b''])
        process.wait = AsyncMock()
        process.returncode = 0
        return process
    monkeypatch.setattr(main.asyncio, 'create_subprocess_exec', launch)
    main.processes['a'] = {'owner': account['owner'], 'process': None, 'cancelled': False}
    asyncio.run(main.execute(account, {'pseudo': 'Alice'}))
    args = commands[0][1:]
    assert '--oauthaccesstoken1' in args and '--oauthaccesstoken2' in args
    assert '--oauth2_token1' not in args and '--oauth2_token2' not in args
    assert 'access-secret' not in main.CONFIG_FILE.read_text()

    # Exercise the real bundled argument parser without connecting to an IMAP
    # server or importing the complete imapsync runtime dependencies.
    perl = shutil.which('perl')
    git_perl = Path('C:/Program Files/Git/usr/bin/perl.exe')
    if not perl and git_perl.exists():
        perl = str(git_perl)
    if not perl:
        pytest.skip('Perl unavailable for the real argument parser check')
    source = Path('imapsync').read_text()
    parser = source[source.index('sub get_options_cmd \n'):source.index('sub tests_get_options \n')]
    harness = env / 'parser.pl'
    harness.write_text('''use English qw(-no_match_vars);
use Getopt::Long ();
sub myGetOptions { shift @ARG; my $args = shift @ARG; return Getopt::Long::GetOptionsFromArray($args, @ARG); }
sub myprint { print @ARG; }
sub output { shift @ARG; print @ARG; }
''' + parser + '\nexit(defined get_options_cmd({}, @ARGV) ? 0 : 64);\n')
    result = subprocess.run([perl, str(harness), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    old_args = [arg.replace('--oauthaccesstoken', '--oauth2_token') for arg in args]
    old = subprocess.run([perl, str(harness), *old_args], capture_output=True, text=True)
    assert old.returncode == 64
    assert 'Unknown option: oauth2_token' in old.stderr


def test_unknown_oauth_argument_error_is_visible(env):
    text = main.clean_log(b'Unknown option: oauth2_token1\naccess-secret\n', ['access-secret'])
    assert 'Unknown option: oauth2_token1' in text
    assert 'access-secret' not in text


def test_oauth_missing_configuration_returns_popup_feedback(env):
    response = client('alice@example.com').get('/oauth/login/microsoft?target_field=oauth2_token2')
    assert response.status_code == 400
    assert 'oauth-error' in response.text and 'postMessage' in response.text
    assert 'oauth2_token2' in response.text
