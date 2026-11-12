"""`society` command line. It talks to the running app (start it with `make dev`)."""

import json
import os

import httpx
import typer

API = os.environ.get('THEBAR_URL', 'http://localhost:8000')
app = typer.Typer(no_args_is_help=True)
run_app = typer.Typer(no_args_is_help=True)
agent_app = typer.Typer(no_args_is_help=True)
app.add_typer(run_app, name='run', help='Create and control research runs.')
app.add_typer(agent_app, name='agent', help='Add, pause, resume, or wake one agent.')


def call(method: str, path: str, **kwargs):
    try:
        r = httpx.request(method, API + path, timeout=60, **kwargs)
    except httpx.ConnectError:
        raise typer.Exit(typer.echo(f'Cannot reach {API}. Start the app with `make dev`.', err=True) or 1)
    if r.status_code >= 400:
        typer.echo(f'Error {r.status_code}: {r.text}', err=True)
        raise typer.Exit(1)
    return r.json()


def show(data) -> None:
    typer.echo(json.dumps(data, indent=2))


def latest_run() -> int:
    runs = [r for r in call('GET', '/api/runs') if r['status'] in ('running', 'paused')]
    if not runs:
        typer.echo('No running or paused run. Pass --run.', err=True)
        raise typer.Exit(1)
    return runs[0]['id']


@run_app.command('start')
def run_start(question: str = typer.Option(None, help='Research question. Defaults to config/society.yaml.')):
    """Create a new run and start it."""
    run_id = call('POST', '/api/runs', json={'question': question})['id']
    run = call('POST', f'/api/runs/{run_id}/start')
    typer.echo(f'Run {run_id}: {run["status"]}')


@run_app.command('list')
def run_list():
    for r in call('GET', '/api/runs'):
        typer.echo(f'{r["id"]:>4}  {r["status"]:<9} {r["created_at"]}  {r["question"][:70]}')


@run_app.command('status')
def run_status(run_id: int):
    run = call('GET', f'/api/runs/{run_id}')
    run.pop('config')
    show(run)


@run_app.command('pause')
def run_pause(run_id: int):
    typer.echo(call('POST', f'/api/runs/{run_id}/pause')['status'])


@run_app.command('resume')
def run_resume(run_id: int):
    typer.echo(call('POST', f'/api/runs/{run_id}/resume')['status'])


@run_app.command('stop')
def run_stop(run_id: int):
    typer.echo(call('POST', f'/api/runs/{run_id}/stop')['status'])


@agent_app.command('add')
def agent_add(agent_id: str, run: int = typer.Option(None, help='Run id. Defaults to the newest running run.')):
    """Add an agent from config/agents/ to an existing run."""
    show(call('POST', f'/api/runs/{run or latest_run()}/agents/{agent_id}/add'))


@agent_app.command('pause')
def agent_pause(agent_id: str, run: int = typer.Option(None, help='Run id. Defaults to the newest running run.')):
    show(call('POST', f'/api/runs/{run or latest_run()}/agents/{agent_id}/pause'))


@agent_app.command('resume')
def agent_resume(agent_id: str, run: int = typer.Option(None, help='Run id. Defaults to the newest running run.')):
    show(call('POST', f'/api/runs/{run or latest_run()}/agents/{agent_id}/resume'))


@agent_app.command('wake')
def agent_wake(agent_id: str, run: int = typer.Option(None, help='Run id. Defaults to the newest running run.')):
    """Start the agent's next activity now."""
    show(call('POST', f'/api/runs/{run or latest_run()}/agents/{agent_id}/wake'))


def main():
    app()
