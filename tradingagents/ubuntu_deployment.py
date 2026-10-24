"""R1.4 export/check only. Stdlib-only so Linux can validate before installation.

No SSH, package installation, user creation or systemctl mutations are performed.
All installation/enable steps require an operator after separate authorization.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'ubuntu-operations-r14-v1'
PHASES = ('research', 'observe', 'paper', 'timely')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def write_json(path, value):
    # Deployment exports are new directories only; runtime uses the existing
    # atomic/sealed writers instead of this preparatory writer.
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


@dataclasses.dataclass(frozen=True)
class Deployment:
    version: str = VERSION
    user: str = 'ta-research'
    release_dir: str = '/opt/tradingagents/releases/r14-reviewed'
    data_dir: str = '/var/lib/tradingagents/r14'
    config_dir: str = '/etc/tradingagents-r14'
    python_minor: str = '3.13'
    min_free_bytes: int = 2147483648
    min_available_memory_bytes: int = 1073741824
    clock_offset_limit_ms: int = 1000
    execution_enabled: bool = False
    allow_new_risk: bool = False
    simulation_admitted: bool = False

    def __post_init__(self):
        if self.version != VERSION or self.python_minor != '3.13': raise ValueError('Unreviewed deployment/runtime version')
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,30}', self.user) or self.user == 'root': raise ValueError('Non-root service user required')
        paths = []
        for raw in (self.release_dir, self.data_dir, self.config_dir):
            path = PurePosixPath(raw)
            if (not re.fullmatch(r'/[A-Za-z0-9_./-]+', raw) or not path.is_absolute()
                or '..' in path.parts or path == PurePosixPath('/') or str(path) != raw):
                raise ValueError('Canonical safe absolute Linux paths required')
            paths.append(path)
        if any(a == b or a in b.parents or b in a.parents for i,a in enumerate(paths) for b in paths[i+1:]):
            raise ValueError('Code, configuration and persistent data must be isolated')
        if any(getattr(self,k) is not False for k in ('execution_enabled','allow_new_risk','simulation_admitted')):
            raise ValueError('Real risk and execution permissions must remain false')
        if type(self.min_free_bytes) is not int or self.min_free_bytes < 1073741824: raise ValueError('Invalid disk floor')
        if type(self.min_available_memory_bytes) is not int or self.min_available_memory_bytes < 536870912: raise ValueError('Invalid memory floor')
        if type(self.clock_offset_limit_ms) is not int or not 100 <= self.clock_offset_limit_ms <= 3000: raise ValueError('Invalid clock tolerance')


def load_deployment(value):
    return Deployment(**value)


def schema():
    properties={}
    for field in dataclasses.fields(Deployment):
        default=getattr(Deployment(),field.name)
        properties[field.name]={'type':'boolean' if isinstance(default,bool) else 'integer' if isinstance(default,int) else 'string','default':default}
    for key in ('execution_enabled','allow_new_risk','simulation_admitted'):properties[key]['const']=False
    properties['version']['const']=VERSION;properties['python_minor']['const']='3.13'
    properties['user']['pattern']='^[a-z][a-z0-9-]{0,30}$'
    return {'$schema':'https://json-schema.org/draft/2020-12/schema','title':'R1.4 deployment preparation',
            'type':'object','additionalProperties':False,'properties':properties,
            'description':'Deployment.__post_init__ additionally validates path isolation, non-root identity and resource floors.'}


def dependency_candidate(root=ROOT):
    """Pin installed versions in the Linux/Python3.13 dependency closure.

    This is NOT a claim of available Linux wheels or a successful Linux resolver.
    No pip freeze, environment dump or authenticated package URL is collected.
    """
    import tomllib
    from packaging.requirements import Requirement
    from packaging.markers import default_environment
    project = tomllib.loads((root/'pyproject.toml').read_text(encoding='utf-8'))
    env = {**default_environment(), 'sys_platform':'linux', 'os_name':'posix',
           'platform_system':'Linux', 'platform_machine':'x86_64',
           'python_version':'3.13', 'python_full_version':'3.13.0'}
    queue = [Requirement(raw) for raw in project['project']['dependencies']]
    pins = {}; seen = set(); issues = []
    while queue:
        req = queue.pop()
        if req.marker and not req.marker.evaluate({**env,'extra':''}): continue
        name = re.sub(r'[-_.]+','-',req.name).lower()
        key = (name, tuple(sorted(req.extras)))
        try: dist = metadata.distribution(req.name)
        except metadata.PackageNotFoundError:
            issues.append({'package':name,'reason':'NOT_INSTALLED_FOR_CANDIDATE_LOCK'}); continue
        if not req.specifier.contains(dist.version):
            issues.append({'package':name,'reason':'INSTALLED_VERSION_OUTSIDE_REQUIREMENT'})
        if req.url: issues.append({'package':name,'reason':'DIRECT_URL_REQUIRES_MANUAL_REVIEW'})
        pins[name] = dist.version
        if key in seen: continue
        seen.add(key)
        for raw in dist.requires or []:
            child = Requirement(raw)
            extras = req.extras or {''}
            if child.marker and not any(child.marker.evaluate({**env,'extra':extra}) for extra in extras): continue
            # Parent extras have already been evaluated for the Linux target.
            child.marker = None
            queue.append(child)
    return {'target':'Linux x86_64 Python 3.13', 'pins':dict(sorted(pins.items())),
            'issues':issues, 'linux_resolver_verified':False,
            'scope':'INSTALLED_VERSION_CANDIDATE_NOT_A_LINUX_WHEEL_OR_HASH_LOCK'}


def services(config, automation_hash, profile_hash, watchlist_hash):
    d = config; release = d.release_dir; bundle = d.config_dir; data = d.data_dir
    plans = {'research':('00,04,08,12,16,20:00:20', 2760, 2048, 10),
             'observe':('*:0/1:02', 90, 1024, 100),
             'paper':('*:0/5:20', 90, 1024, 100),
             'timely':('*:58:40', 150, 1024, 1000)}
    result = {}
    for phase,(calendar,timeout,memory,weight) in plans.items():
        service = f'''[Unit]
Description=TradingAgents R1.4 {phase} RESEARCH/PAPER ONLY
Wants=network-online.target
After=network-online.target time-sync.target

[Service]
Type=oneshot
User={d.user}
Group={d.user}
WorkingDirectory={release}
UMask=0077
Environment=HOME={data}/home TZ=UTC PYTHONUTF8=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 XDG_CACHE_HOME={data}/cache
Environment=GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0={release}
EnvironmentFile=-{bundle}/model.env
ExecStart={release}/.venv/bin/python -m tradingagents.ubuntu_operations run {phase} --bundle {bundle}
TimeoutStartSec={timeout}
TimeoutStopSec=10
KillMode=control-group
Restart=no
TasksMax=128
MemoryMax={memory}M
CPUWeight={weight}
Nice={'10' if phase == 'research' else '0'}
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectClock=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths={data}
StandardOutput=append:{data}/logs/{phase}.log
StandardError=null
'''
        timer = f'''[Unit]
Description=TradingAgents R1.4 {phase} timer (operator enable only)

[Timer]
OnCalendar=*-*-* {calendar} UTC
AccuracySec=1s
RandomizedDelaySec=0
Persistent=false
Unit=ta-r14-{phase}.service

[Install]
WantedBy=timers.target
'''
        result['ta-r14-'+phase+'.service'] = service
        result['ta-r14-'+phase+'.timer'] = timer
    result['tradingagents-r14.logrotate'] = f'''{data}/logs/*.log {{
    daily
    maxsize 10M
    rotate 7
    missingok
    notifempty
    compress
    copytruncate
    su {d.user} {d.user}
}}
'''
    # An explicit, optional, standalone probe. It never calls fill/settle or
    # writes F1/F2/F3. Not in the operational timer install/enable list.
    result['ta-r14-probe.service'] = result['ta-r14-timely.service'].replace(
        'run timely --bundle '+bundle, 'run-probe --bundle '+bundle+' --plan '+data+'/probes/plan.json').replace(
        '/logs/timely.log','/logs/probe.log')
    result['ta-r14-probe.timer'] = result['ta-r14-timely.timer'].replace('ta-r14-timely.service','ta-r14-probe.service')
    return result


def source_manifest(root=ROOT):
    tracked = subprocess.check_output(['git','ls-files'], cwd=root, text=True).splitlines()
    required = [p for p in tracked if p.startswith(('tradingagents/','cli/')) or p in
                ('pyproject.toml','scripts/probe_okx_llm.py') or p.startswith('docs/') and p.endswith('.json')]
    required += ['tradingagents/ubuntu_deployment.py','tradingagents/ubuntu_operations.py']
    return {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in sorted(set(required))}


def prepare(output, config):
    # Existing schemas remain the source of truth. No extra scheduler/strategy.
    from tradingagents import automation as a, auto_research as r11, paper_instruments as r13
    output = Path(output)
    if output.exists(): raise ValueError('New export directory required; no overwrite')
    watch = r11.Watchlist.model_validate(json.loads((ROOT/'docs/auto_research_r11.json').read_text(encoding='utf-8')))
    profiles = r13.load_config(r13.DEFAULT_CONFIG)
    if watch.enabled() != ['BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP']: raise ValueError('Review initial watchlist')
    if [p.instrument for p in profiles.profiles if p.enabled] != watch.enabled(): raise ValueError('Paper admission/watchlist mismatch')
    auto = a.AutomationConfig(watchlist=config.config_dir+'/watchlist.json',
          batch_root=config.data_dir+'/batch', state_root=config.data_dir+'/automation',
          sources=[a.ArchiveSource(name='ubuntu-live', registry=None, optional=True)])
    deps = dependency_candidate()
    if deps['issues']: raise ValueError('Dependency candidate incomplete; resolve before export')
    output.mkdir(parents=True)
    values = {'deployment.json':dataclasses.asdict(config),'automation.json':auto.model_dump(),
              'watchlist.json':watch.model_dump(),'instruments.json':profiles.model_dump(),
              'dependency-candidate.json':deps}
    for name,value in values.items(): write_json(output/name,value)
    lock=ROOT/'docs/ubuntu_r14.requirements.lock'
    lock_info=json.loads((ROOT/'docs/ubuntu_r14_dependency_lock.json').read_text(encoding='utf-8'))
    if hashlib.sha256(lock.read_bytes()).hexdigest()!=lock_info['lock_sha256']:raise ValueError('Linux lock fingerprint changed')
    shutil.copyfile(lock,output/'requirements-linux.lock')
    write_json(output/'dependency-lock.json',lock_info)
    for name,body in services(config, a.digest(auto.model_dump()), a.digest(profiles.model_dump()), a.digest(watch.model_dump())).items():
        (output/name).write_text(body, encoding='utf-8', newline='\n')
    (output/'requirements-linux-candidate.txt').write_text(
        '# Candidate only: validate resolver, wheels and pip check on Linux before enabling.\n'+
        '\n'.join(name+'=='+version for name,version in deps['pins'].items())+'\n',encoding='utf-8')
    (output/'model.env.example').write_text(
        '# Copy manually outside Git; chmod 600. No exchange credentials needed.\n'
        'TRADINGAGENTS_LLM_PROVIDER=chatgpt\n'
        'TRADINGAGENTS_QUICK_THINK_LLM=gpt-6.1-sol\n'
        'TRADINGAGENTS_DEEP_THINK_LLM=gpt-6.1-sol\n'
        '# Do not copy Windows localhost proxy settings to Ubuntu.\n',encoding='utf-8')
    manifest = {'version':VERSION,'prepared_ms':time.time_ns()//1000000,
                'prepared_platform':platform.system(),'prepared_python':platform.python_version(),
                'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                'source_sha256':source_manifest(),
                'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()},
                'native_automation_hash':a.digest(auto.model_dump()),'native_profile_hash':a.digest(profiles.model_dump()),
                'native_watchlist_hash':a.digest(watch.model_dump()),'installed':False,'timers_enabled':False,
                'linux_dependency_resolution_verified':True,'actual_linux_install_verified':False,'server_deployment_verified':False,
                'execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False}
    manifest['manifest_hash'] = fingerprint(manifest)
    write_json(output/'manifest.json',manifest)
    return {'status':'PREPARED_NOT_INSTALLED','output':str(output.resolve()),
            'manifest_hash':manifest['manifest_hash'],'files':len(manifest['files']),
            'dependency_pins':len(deps['pins']),'server_deployment_verified':False,'execution_enabled':False}


def validate_bundle(directory, *, source_root=None):
    directory = Path(directory)
    manifest = json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('manifest_hash') != fingerprint({k:v for k,v in manifest.items() if k != 'manifest_hash'}):
        raise ValueError('Deployment manifest fingerprint mismatch')
    for name,expected in manifest['files'].items():
        if Path(name).name != name: raise ValueError('Unsafe bundle filename')
        if hashlib.sha256((directory/name).read_bytes()).hexdigest() != expected: raise ValueError('Bundle file fingerprint mismatch')
    config = load_deployment(json.loads((directory/'deployment.json').read_text(encoding='utf-8')))
    if source_root is not None:
        for name,expected in manifest['source_sha256'].items():
            if '..' in PurePosixPath(name).parts or PurePosixPath(name).is_absolute(): raise ValueError('Unsafe source path')
            if hashlib.sha256((Path(source_root)/name).read_bytes()).hexdigest() != expected:
                raise ValueError('Source bytes differ; prepare/review on target platform, never bypass')
    return config, manifest


def host_check(config, *, phase=None):
    """Metadata only: never read auth content or dump proxy/environment values."""
    issues = []
    linux = platform.system() == 'Linux'
    if not linux: issues.append('LINUX_REQUIRED')
    distribution={};systemd_version=None
    if linux:
        try:
            for line in Path('/etc/os-release').read_text().splitlines():
                if '=' in line:
                    key,value=line.split('=',1)
                    if key in {'ID','VERSION_ID'}:distribution[key]=value.strip('"')
            answer=subprocess.run(['systemd-analyze','--version'],capture_output=True,text=True,timeout=3,check=False)
            if answer.returncode==0:systemd_version=int(answer.stdout.splitlines()[0].split()[1])
        except (OSError,ValueError,IndexError,subprocess.TimeoutExpired):pass
        if distribution.get('ID')!='ubuntu':issues.append('UBUNTU_NOT_CONFIRMED')
        if systemd_version is None or systemd_version<255:issues.append('SYSTEMD_VERSION_NOT_VALIDATED')
        if platform.machine().lower() not in {'x86_64','amd64'}:issues.append('LINUX_ARCHITECTURE_NOT_VALIDATED')
    uid = os.geteuid() if hasattr(os,'geteuid') else None
    if uid in (None,0): issues.append('NON_ROOT_REQUIRED')
    if linux and uid is not None:
        import pwd
        if pwd.getpwuid(uid).pw_name!=config.user:issues.append('WRONG_SERVICE_USER')
    if '.'.join(map(str,sys.version_info[:2])) != config.python_minor: issues.append('PYTHON_MINOR_NOT_VALIDATED')
    ntp = 'UNAVAILABLE'
    if linux:
        try:
            answer = subprocess.run(['timedatectl','show','--property=NTPSynchronized','--value'],
                                    capture_output=True,text=True,timeout=3,check=False)
            ntp = 'SYNCED' if answer.returncode==0 and answer.stdout.strip()=='yes' else 'NOT_CONFIRMED'
        except (OSError,subprocess.TimeoutExpired): pass
    if ntp != 'SYNCED': issues.append('NTP_NOT_CONFIRMED')
    if linux and os.environ.get('TZ')!='UTC':issues.append('PROCESS_UTC_REQUIRED')
    parent = Path(config.data_dir)
    while not parent.exists() and parent != parent.parent: parent = parent.parent
    free = shutil.disk_usage(parent).free if parent.exists() else None
    if free is None or free < config.min_free_bytes: issues.append('INSUFFICIENT_DISK')
    available = None
    if linux:
        try:
            for line in Path('/proc/meminfo').read_text().splitlines():
                if line.startswith('MemAvailable:'): available = int(line.split()[1])*1024
        except (OSError,ValueError): pass
    if phase=='research' and (available is None or available < config.min_available_memory_bytes): issues.append('INSUFFICIENT_RESEARCH_MEMORY')
    auth = Path.home()/'.tradingagents/chatgpt-auth.json'
    auth_metadata = {'exists':auth.is_file(),'content_read':False,'authentication_verified':False}
    if auth.is_file() and linux:
        auth_metadata['private_mode'] = auth.stat().st_mode & 0o077 == 0
        if not auth_metadata['private_mode']: issues.append('AUTH_FILE_NOT_PRIVATE')
    provider = os.environ.get('TRADINGAGENTS_LLM_PROVIDER','chatgpt')
    if phase=='research' and provider=='chatgpt' and not auth_metadata['exists']: issues.append('PROJECT_MODEL_LOGIN_REQUIRED')
    # Presence only, never proxy URLs/passwords. Local Windows tunnels cannot be
    # assumed to exist in the service environment.
    proxy_presence = {key:bool(os.environ.get(key)) for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY',
                     'http_proxy','https_proxy','all_proxy','no_proxy','TRADINGAGENTS_LLM_PROXY')}
    return {'platform':platform.system(),'python':platform.python_version(),'uid':uid,
            'distribution':distribution,'machine':platform.machine(),'systemd_version':systemd_version,
            'ntp':ntp,'process_utc':os.environ.get('TZ')=='UTC','free_bytes':free,'available_memory_bytes':available,'auth':auth_metadata,
            'proxy_variable_presence':proxy_presence,'issues':issues,'ready':not issues,
            'server_deployment_verified':False,'execution_enabled':False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','validate','check'])
    parser.add_argument('--settings',type=Path,default=ROOT/'docs/ubuntu_r14.json')
    parser.add_argument('--bundle',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--source-root',type=Path)
    args = parser.parse_args()
    try:
        if args.command=='prepare':
            if not args.output: raise ValueError('Explicit new export directory required')
            result=prepare(args.output,load_deployment(json.loads(args.settings.read_text(encoding='utf-8'))))
        else:
            if not args.bundle: raise ValueError('Explicit bundle required')
            config,manifest=validate_bundle(args.bundle,source_root=args.source_root)
            result=({'status':'BUNDLE_FINGERPRINTS_VALID','manifest_hash':manifest['manifest_hash'],
                     'server_deployment_verified':False,'execution_enabled':False} if args.command=='validate' else host_check(config))
        print(json.dumps(result,ensure_ascii=True,indent=2))
    except Exception as exc:
        print(json.dumps({'status':'R14_PREPARATION_UNAVAILABLE','error_type':type(exc).__name__,'execution_enabled':False}))
        raise SystemExit(2)


if __name__=='__main__': main()
