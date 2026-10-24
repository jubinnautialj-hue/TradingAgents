"""Local Linux compatibility evidence, no installation or service registration.

Uses only stdlib and disposable fixtures. Executable/unit paths in the systemd
syntax fixture are NOT a real production venv. Does not claim server readiness.
"""
import argparse
import importlib.util
import json
import multiprocessing
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module


files=load('r14_native_files',ROOT/'tradingagents/dataflows/files.py')
d=load('r14_deployment_stdlib',ROOT/'tradingagents/ubuntu_deployment.py')


def append_once(path):
    with files.locked(path):
        current=json.loads(Path(path).read_text()) if Path(path).exists() else []
        if 'same-bar' not in current:
            current.append('same-bar');files.replace_file(path,lambda temp:Path(temp).write_text(json.dumps(current)))


def hold(path,ready):
    with files.locked(path):ready.put(True);__import__('time').sleep(20)


def verify():
    if sys.platform!='linux' or os.geteuid()==0:raise ValueError('Run locally on Linux as an unprivileged user')
    ctx=multiprocessing.get_context('fork')
    with tempfile.TemporaryDirectory(prefix='tradingagents-r14-verify-') as raw:
        root=Path(raw);path=root/'events.json'
        jobs=[ctx.Process(target=append_once,args=(path,)) for _ in range(2)]
        for job in jobs:job.start()
        for job in jobs:job.join(5);assert job.exitcode==0
        assert json.loads(path.read_text())==['same-bar']
        queue=ctx.Queue();job=ctx.Process(target=hold,args=(root/'crash',queue));job.start();assert queue.get(timeout=3)
        job.terminate();job.join(3)
        with files.locked(root/'crash',wait=False) as acquired:assert acquired
        # Isolated syntax fixture with an existing Python executable path;
        # never invokes ExecStart or creates a system user/unit registration.
        release=root/'release';(release/'.venv/bin').mkdir(parents=True)
        (release/'.venv/bin/python').symlink_to(sys.executable)
        data=root/'data';data.mkdir();(data/'logs').mkdir();config=root/'config';config.mkdir()
        settings=d.Deployment(user=pwd.getpwuid(os.geteuid()).pw_name,release_dir=str(release),data_dir=str(data),config_dir=str(config))
        bodies=d.services(settings,'syntax','syntax','syntax');units=[];calendars=[]
        for name,body in bodies.items():
            path=root/name;path.write_text(body)
            if path.suffix in {'.service','.timer'}:units.append(str(path))
            if path.suffix=='.timer':calendars.extend(line.split('=',1)[1] for line in body.splitlines() if line.startswith('OnCalendar='))
        result=subprocess.run(['systemd-analyze','verify','--man=no',*units],capture_output=True,text=True,timeout=15)
        syntax={'returncode':result.returncode,'scope':'DISPOSABLE_NON_PRODUCTION_PATHS_PYTHON_SYMLINK_ONLY_NOT_A_VENV'}
        if result.returncode:syntax['diagnostic']=result.stderr[:3000]
        calendar_ok=[]
        for expression in calendars:
            checked=subprocess.run(['systemd-analyze','calendar',expression],capture_output=True,text=True,timeout=5)
            calendar_ok.append(checked.returncode==0)
        rotate=subprocess.run(['logrotate','--debug','--state',str(root/'logrotate.state'),str(root/'tradingagents-r14.logrotate')],
                              capture_output=True,text=True,timeout=10)
        return {'platform':sys.platform,'python':sys.version.split()[0],'uid':os.geteuid(),
                'native_posix_lock_dedup':True,'lock_released_after_process_termination':True,
                'systemd_version':subprocess.check_output(['systemd-analyze','--version'],text=True).splitlines()[0],
                'unit_syntax_fixture':syntax,'all_calendars_valid':all(calendar_ok),'calendar_cases':len(calendar_ok),
                'logrotate_debug_returncode':rotate.returncode,'units_installed':0,'services_started':0,
                'runtime_python313_or_app_import_verified':False,'server_deployment_verified':False,'execution_enabled':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    value=verify();args.output.write_text(json.dumps(value,indent=2)+'\n');print(json.dumps(value))
