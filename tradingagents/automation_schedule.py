"""Export-only OS scheduling templates and credential-free latency evidence."""
from __future__ import annotations

import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from tradingagents.automation import ROOT,expand,PHASES
from tradingagents import auto_research as r11,forward_tracking as f2,forward_paper as paper
from tradingagents.forward_validation import digest,atomic


def next_trigger(now_ms,period_seconds,offset_seconds):
    period=period_seconds*1000
    result=now_ms//period*period+offset_seconds*1000
    return result if result>now_ms else result+period


def export(cfg,config_path,directory):
    directory=Path(directory or expand(cfg.state_root)/'schedule_export').resolve()
    directory.mkdir(parents=True,exist_ok=True)
    manifest={'config_hash':digest(cfg.model_dump()),'installed':False,'files':[],'execution_enabled':False}
    plans={'research':(cfg.schedule.research_seconds,20,7500),'observe':(cfg.schedule.observe_seconds,2,90),
           'paper':(cfg.schedule.paper_seconds,20,90),'timely':(3600,3520,180)}
    ns='http://schemas.microsoft.com/windows/2004/02/mit/task';ET.register_namespace('',ns)
    def tag(name):return '{'+ns+'}'+name
    now=r11.utc_ms()
    for phase,(period,offset,timeout) in plans.items():
        task=ET.Element(tag('Task'),{'version':'1.2'})
        info=ET.SubElement(task,tag('RegistrationInfo'));ET.SubElement(info,tag('Description')).text='R1.2 '+phase+' — research/paper only, never exchange orders'
        triggers=ET.SubElement(task,tag('Triggers'));trigger=ET.SubElement(triggers,tag('TimeTrigger'))
        repetition=ET.SubElement(trigger,tag('Repetition'));ET.SubElement(repetition,tag('Interval')).text='PT'+str(period)+'S'
        ET.SubElement(repetition,tag('StopAtDurationEnd')).text='false'
        start=next_trigger(now,period,offset)
        ET.SubElement(trigger,tag('StartBoundary')).text=datetime.fromtimestamp(start/1000,UTC).isoformat(timespec='seconds')
        ET.SubElement(trigger,tag('Enabled')).text='true'
        principals=ET.SubElement(task,tag('Principals'));principal=ET.SubElement(principals,tag('Principal'),{'id':'Author'})
        ET.SubElement(principal,tag('LogonType')).text='InteractiveToken';ET.SubElement(principal,tag('RunLevel')).text='LeastPrivilege'
        settings=ET.SubElement(task,tag('Settings'))
        for name,value in [('MultipleInstancesPolicy','IgnoreNew'),('DisallowStartIfOnBatteries','false'),
                           ('StopIfGoingOnBatteries','false'),('AllowHardTerminate','true'),('StartWhenAvailable','false'),
                           ('RunOnlyIfNetworkAvailable','false'),('AllowStartOnDemand','true'),('Enabled','true'),
                           ('Hidden','false'),('ExecutionTimeLimit','PT'+str(timeout)+'S'),('Priority','5')]:
            ET.SubElement(settings,tag(name)).text=value
        actions=ET.SubElement(task,tag('Actions'),{'Context':'Author'});action=ET.SubElement(actions,tag('Exec'))
        ET.SubElement(action,tag('Command')).text=str(Path(sys.executable).resolve())
        ET.SubElement(action,tag('Arguments')).text=subprocess.list2cmdline(['-m','tradingagents.automation',phase,'--config',str(Path(config_path).resolve()),'--expected-config-hash',manifest['config_hash']])
        ET.SubElement(action,tag('WorkingDirectory')).text=str(ROOT)
        # Task Scheduler COM consumes UTF-16 BSTR XML; matching its native
        # declaration avoids an encoding-switch error during in-memory validation.
        file=directory/('r12-'+phase+'.xml');ET.indent(task);ET.ElementTree(task).write(file,encoding='utf-16',xml_declaration=True)
        ET.parse(file);manifest['files'].append(str(file))
        hours=','.join(f'{h:02}' for h in range(0,24,cfg.schedule.research_seconds//3600))
        calendar={'research':'*-*-* '+hours+':00:20 UTC',
                  'observe':'*-*-* *:0/'+str(cfg.schedule.observe_seconds//60)+':02 UTC',
                  'paper':'*-*-* *:0/'+str(cfg.schedule.paper_seconds//60)+':20 UTC','timely':'*-*-* *:58:40 UTC'}[phase]
        service='[Unit]\nDescription=TradingAgents R1.2 '+phase+' paper research only\nAfter=network-online.target time-sync.target\n\n[Service]\nType=oneshot\nWorkingDirectory=%h/TradingAgents\nExecStart="%h/TradingAgents/.venv/bin/python" -m tradingagents.automation '+phase+' --config "%h/TradingAgents/docs/automation_r12.json" --expected-config-hash '+manifest['config_hash']+'\nTimeoutStartSec='+str(timeout)+'\nTimeoutStopSec=10\nKillMode=control-group\nRestart=no\n'
        timer='[Unit]\nDescription=R1.2 '+phase+' export-only timer\n\n[Timer]\nOnCalendar='+calendar+'\nAccuracySec=1s\nRandomizedDelaySec=0\nPersistent=false\nUnit=r12-'+phase+'.service\n\n[Install]\nWantedBy=timers.target\n'
        for suffix,body in [('.service',service),('.timer',timer)]:
            target=directory/('r12-'+phase+suffix);target.write_text(body,encoding='utf-8');manifest['files'].append(str(target))
    atomic(directory/'manifest.json',manifest)
    return manifest


def public_probe(cfg):
    """Unbound official market probe. Never create a candidate/condition/fill."""
    limits=f2.ScanLimits(max_requests=8,max_seconds=30)
    client=f2.PublicBudgetClient(limits);market=paper.Market(client,limits)
    server=market.now();instruments=[]
    for symbol in ('BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP'):
        info=client.instrument(symbol)
        instruments.append({'instrument':symbol,'state':info['state'],'settlement':info['settleCcy']})
    t=time.monotonic();end=server//paper.HOUR*paper.HOUR
    batch=market.bars('BTC-USDT-SWAP',end-2*paper.HOUR,end)
    rows=client.get('/api/v5/market/candles',{'instId':'BTC-USDT-SWAP','bar':'1H','limit':'2'})
    latency=(time.monotonic()-t)*1000
    result={'status':'PUBLIC_PROBE_COMPLETE','scope':'UNBOUND_LATENCY_ONLY_NOT_A_TIMELY_FILL',
            'server_ms':server,'instruments':instruments,'confirmed_bars':len(batch['bars']),
            'current_open_ms':int(rows[0][0]),'current_confirm':rows[0][8],
            'warm_closed_plus_open_fetch_ms':latency,'observed_fetch_under_15s':latency<=15000,
            'logical_get_calls':client.calls,'fills':0,'llm_calls':0,'execution_enabled':False,
            'limitations':['Not an eligible candidate or actual scheduled-boundary fill','Latency sample does not guarantee OS dispatch, disk verification or future network performance']}
    directory=expand(cfg.state_root)/'public_probes';directory.mkdir(parents=True,exist_ok=True)
    atomic(directory/(digest(result)+'.json'),result)
    return result
