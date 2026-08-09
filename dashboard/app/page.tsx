"use client";

import { useEffect, useMemo, useState } from "react";

type NewsSource = { title:string; url:string; publisher:string };
type Signal = {
  question:string; side:string; confidence:number; tier?:string|null;
  historyScore:number; historyScope:string; historySamples:number;
  newsScore:number; newsCount:number; newsEntity:string; newsSources:NewsSource[];
  modelEdge:number; marketPrior?:number; timingScore?:number;
  liquidity?:number; volume?:number; bookSpread?:number; entryAsk?:number;
  crossBookArb:number; arbConfidence?:number; arbQualified?:boolean;
  arbExecutable?:boolean; arbGate?:string; route?:string; qualified:boolean; gate:string;
};
type Control = {
  paused:boolean; minimumConfidence:number; minModelEdgePct:number;
  minTimingScore:number; maxHoursBeforeEvent:number; newsEnabled:boolean; updatedAt?:string;
};
type Status = {
  connected?:boolean; stale?:boolean; mode?:string; markets?:number; positions?:number;
  deployed?:number; dailyPnl?:number; lastCycle?:string; signals?:Signal[];
  evidence?:{gammaObservations?:number;transcriptSources?:Record<string,{documents:number;mentions:number}>};
  strategy?:{directional?:boolean;arbDetection?:boolean;arbExecution?:boolean;maxPositionsPerContract?:number};
  control?:Control;
};

const defaultControl:Control = {
  paused:false, minimumConfidence:65, minModelEdgePct:6,
  minTimingScore:45, maxHoursBeforeEvent:4, newsEnabled:true,
};
const tags = [["105481","Trump speech"],["105482","Politics"],["105486","NFL"],["100343","Mentions"]];

export default function Home(){
  const [status,setStatus]=useState<Status>({connected:false});
  const [query,setQuery]=useState("");
  const [filter,setFilter]=useState<"all"|"qualified"|"arb"|"blocked">("all");
  const [sort,setSort]=useState<"confidence"|"edge"|"timing"|"arb">("confidence");
  const [controlOpen,setControlOpen]=useState(false);
  const [control,setControl]=useState<Control>(defaultControl);
  const [controlConfigured,setControlConfigured]=useState(false);
  const [adminToken,setAdminToken]=useState("");
  const [saving,setSaving]=useState(false);
  const [notice,setNotice]=useState("");

  useEffect(()=>{
    const load=()=>fetch("/api/status",{cache:"no-store"}).then(r=>r.json()).then((next:Status)=>{
      setStatus(next);
      if(next.control)setControl({...defaultControl,...next.control});
    }).catch(()=>setStatus({connected:false}));
    load(); const timer=setInterval(load,15000); return()=>clearInterval(timer);
  },[]);
  useEffect(()=>{
    fetch("/api/control",{cache:"no-store"}).then(r=>r.json()).then(data=>{
      setControlConfigured(Boolean(data.configured));
      if(data.control)setControl({...defaultControl,...data.control});
    }).catch(()=>undefined);
  },[]);
  useEffect(()=>{
    const close=(event:KeyboardEvent)=>{if(event.key==="Escape")setControlOpen(false)};
    window.addEventListener("keydown",close); return()=>window.removeEventListener("keydown",close);
  },[]);

  const signals=status.signals??[];
  const visible=useMemo(()=>signals.filter(signal=>{
    const text=signal.question.toLowerCase().includes(query.toLowerCase());
    const group=filter==="all" || (filter==="qualified"&&signal.qualified)
      || (filter==="arb"&&Boolean(signal.arbQualified))
      || (filter==="blocked"&&!signal.qualified&&!signal.arbQualified);
    return text&&group;
  }).sort((a,b)=>{
    const value=(signal:Signal)=>sort==="edge"?signal.modelEdge
      :sort==="timing"?(signal.timingScore??0)
      :sort==="arb"?(signal.arbConfidence??0):signal.confidence;
    return value(b)-value(a);
  }),[signals,query,filter,sort]);
  const best=visible[0]??signals[0];
  const connected=Boolean(status.connected&&!status.stale);
  const qualified=signals.filter(signal=>signal.qualified).length;
  const arbWatch=signals.filter(signal=>signal.arbQualified).length;
  const evidenceCount=status.evidence?.gammaObservations??0;

  async function saveControl(){
    setSaving(true); setNotice("");
    try{
      const response=await fetch("/api/control",{
        method:"POST", headers:{"Content-Type":"application/json","Authorization":`Bearer ${adminToken}`},
        body:JSON.stringify(control),
      });
      const data=await response.json();
      if(!response.ok)throw new Error(data.error||"Could not save controls");
      setControl(data.control); setNotice("Saved. The bot will apply this on its next cycle.");
      setAdminToken("");
    }catch(error){setNotice(error instanceof Error?error.message:"Could not save controls")}
    finally{setSaving(false)}
  }

  return <main>
    <header className="topbar">
      <a className="brand" href="#top" aria-label="Mention Edge home"><span className="brandMark">M</span><span><b>MENTION EDGE</b><small>Live execution console</small></span></a>
      <nav><a href="#radar">Radar</a><a href="#evidence">Evidence</a><a href="#system">System</a></nav>
      <div className="topActions"><span className={`health ${connected?"online":""}`}><i/>{connected?"LIVE FEED":"DISCONNECTED"}</span><button className="controlButton" onClick={()=>setControlOpen(true)}>Control room <span>⌘</span></button></div>
    </header>

    <section className={`safetyBar ${control.paused?"paused":""}`} id="top">
      <span>{control.paused?"Ⅱ":"●"}</span><div><b>{control.paused?"NEW ENTRIES PAUSED":"LIVE TRADING ARMED"}</b><small>{control.paused?"Existing positions are still monitored and managed.":"Every order must clear confidence, edge, timing, price, liquidity, and exposure gates."}</small></div>
      <em>{status.mode||"LIVE"}</em>
    </section>

    <section className="hero">
      <div className="heroCopy"><p className="eyebrow">REAL-TIME MENTION INTELLIGENCE</p><h1>See the signal.<br/><em>Know the reason.</em></h1><p>One calm command center for historical patterns, grounded news, live book pressure, pricing edge, and maker-first execution.</p><div className="heroActions"><a href="#radar">Explore live radar</a><button onClick={()=>setControlOpen(true)}>Tune strategy</button></div></div>
      <div className="signalOrb" aria-label={best?`Leading signal ${best.confidence.toFixed(1)} percent`:"No signal available"} style={{"--score":`${best?.confidence??0}%`} as React.CSSProperties}>
        <div><span>LEADING SIGNAL</span><strong>{best?`${best.confidence.toFixed(1)}%`:"—"}</strong><small>{best?`${best.side} · ${best.tier??"WATCH"}`:"WAITING FOR CYCLE"}</small></div>
      </div>
    </section>

    <section className="metrics" aria-label="Bot metrics">
      <Metric label="Markets scanned" value={connected?String(status.markets??0):"—"} note="Latest complete cycle" trend="↗ live"/>
      <Metric label="Qualified now" value={connected?String(qualified):"—"} note="Directional candidates" trend={`${arbWatch} arb watch`}/>
      <Metric label="Open positions" value={connected?String(status.positions??0):"—"} note="Maximum five total" trend={`$${(status.deployed??0).toFixed(2)} deployed`}/>
      <Metric label="Daily P&L" value={connected?`${(status.dailyPnl??0)>=0?"+":""}$${(status.dailyPnl??0).toFixed(2)}`:"—"} note="Reporting, no cutoff" trend="live wallet" accent/>
    </section>

    <section className="workspace" id="radar">
      <article className="panel radarPanel">
        <div className="panelHead"><div><p className="eyebrow">OPPORTUNITY RADAR</p><h2>Evaluated contracts</h2></div><span>{signals.length} signals</span></div>
        <div className="toolbar">
          <label className="search"><span>⌕</span><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="Search a market or phrase…" aria-label="Search evaluated markets"/></label>
          <div className="filters" aria-label="Filter signals">{(["all","qualified","arb","blocked"] as const).map(item=><button key={item} className={filter===item?"active":""} onClick={()=>setFilter(item)}>{item}</button>)}</div>
          <label className="sort">Sort <select value={sort} onChange={e=>setSort(e.target.value as typeof sort)}><option value="confidence">Confidence</option><option value="edge">Model edge</option><option value="timing">Timing</option><option value="arb">Arb confidence</option></select></label>
        </div>
        <div className="signalList" id="evidence">{visible.length?visible.map((signal,index)=><SignalRow signal={signal} key={`${signal.question}-${index}`}/>):<div className="empty"><span>◎</span><b>No signals match this view</b><small>Clear the search or choose another filter.</small></div>}</div>
      </article>

      <aside className="sideStack">
        <article className="panel strategyCard">
          <div className="panelHead"><div><p className="eyebrow">ACTIVE STRATEGY</p><h2>Guardrail stack</h2></div><button onClick={()=>setControlOpen(true)}>Edit</button></div>
          <div className="strategyState"><span className={control.paused?"amber":"green"}>{control.paused?"PAUSED":"ARMED"}</span><small>Changes apply on the next scan</small></div>
          <Gauge label="Minimum confidence" value={control.minimumConfidence} min={50} max={100} suffix="%"/>
          <Gauge label="Model edge" value={control.minModelEdgePct} min={0} max={20} suffix="%"/>
          <Gauge label="Timing confirmation" value={control.minTimingScore} min={0} max={100}/>
          <div className="miniRules"><div><span>ENTRY WINDOW</span><b>≤ {control.maxHoursBeforeEvent}h</b></div><div><span>NEWS SIGNAL</span><b>{control.newsEnabled?"ACTIVE":"EXCLUDED"}</b></div><div><span>ARBITRAGE</span><b>WATCH ONLY</b></div><div><span>TIER C</span><b>65–79 · $3</b></div></div>
        </article>

        <article className="panel evidenceCard">
          <div className="panelHead"><div><p className="eyebrow">DATA HEALTH</p><h2>Evidence engine</h2></div><span>{evidenceCount.toLocaleString()} learned</span></div>
          <div className="sourceHealth"><Source name="Gamma history" detail={`${evidenceCount.toLocaleString()} resolved observations`} on/><Source name="Grounded news" detail={control.newsEnabled?"Included only when matched":"Excluded by control"} on={control.newsEnabled}/><Source name="Official transcripts" detail="GovInfo context counts" on/><Source name="TV subtitles" detail="Optional historical source" on={Boolean(status.evidence?.transcriptSources)}/></div>
        </article>
      </aside>
    </section>

    <section className="system panel" id="system">
      <div><p className="eyebrow">SYSTEM MAP</p><h2>From evidence to execution</h2><p>Directional probability and execution quality stay separate so book conditions cannot manufacture outcome certainty.</p></div>
      <div className="routeFlow"><RouteStep number="01" title="Estimate" text="History + grounded news + market prior"/><i>→</i><RouteStep number="02" title="Confirm" text="Order book + momentum timing"/><i>→</i><RouteStep number="03" title="Gate" text="Edge + liquidity + exposure + price"/><i>→</i><RouteStep number="04" title="Execute" text="Maker first, bounded taker fallback"/></div>
      <div className="tagRail">{tags.map(([id,name])=><span key={id}><b>{name}</b><small>#{id} · verified</small></span>)}</div>
    </section>

    <footer><span>MENTION EDGE · OWNER-CONTROLLED LIVE EXECUTION</span><span>Last cycle: {formatCycle(status.lastCycle)}</span></footer>

    {controlOpen&&<div className="modalBackdrop" onMouseDown={event=>{if(event.currentTarget===event.target)setControlOpen(false)}}>
      <section className="controlModal" role="dialog" aria-modal="true" aria-labelledby="control-title">
        <div className="modalHead"><div><p className="eyebrow">AUTHENTICATED CONTROLS</p><h2 id="control-title">Strategy control room</h2><p>Only whitelisted guardrails can change. Wallet credentials are never available here.</p></div><button onClick={()=>setControlOpen(false)} aria-label="Close control room">×</button></div>
        {!controlConfigured&&<div className="warning"><b>Admin controls are not configured.</b><span>Add a 24+ character DASHBOARD_ADMIN_TOKEN to the VPS environment, then rebuild the dashboard.</span></div>}
        <div className="controlGrid">
          <ControlRange label="Minimum confidence" help="Tier C cannot go below 65%." value={control.minimumConfidence} min={65} max={90} step={1} suffix="%" onChange={value=>setControl({...control,minimumConfidence:value})}/>
          <ControlRange label="Minimum model edge" help="The directional price advantage must remain at least 6%." value={control.minModelEdgePct} min={6} max={20} step={0.5} suffix="%" onChange={value=>setControl({...control,minModelEdgePct:value})}/>
          <ControlRange label="Timing confirmation" help="Higher values demand stronger book and momentum agreement." value={control.minTimingScore} min={45} max={90} step={1} onChange={value=>setControl({...control,minTimingScore:value})}/>
          <ControlRange label="Entry window" help="How close the known event start must be." value={control.maxHoursBeforeEvent} min={1} max={4} step={0.5} suffix="h" onChange={value=>setControl({...control,maxHoursBeforeEvent:value})}/>
        </div>
        <div className="switchRows"><Toggle checked={control.newsEnabled} onChange={newsEnabled=>setControl({...control,newsEnabled})} title="Use grounded news" text="No matching articles are treated as unavailable, never as a 50 score."/><Toggle checked={control.paused} onChange={paused=>setControl({...control,paused})} title="Pause new entries" text="Position monitoring and protective exits continue while paused." danger/></div>
        <label className="tokenField"><span>Dashboard admin token</span><input type="password" autoComplete="off" value={adminToken} onChange={e=>setAdminToken(e.target.value)} placeholder="Enter token to authorize this save"/></label>
        {notice&&<p className={`notice ${notice.startsWith("Saved")?"success":"error"}`}>{notice}</p>}
        <div className="modalActions"><button onClick={()=>{setControl(status.control??defaultControl);setNotice("")}}>Reset draft</button><button className="primary" disabled={!controlConfigured||adminToken.length<24||saving} onClick={saveControl}>{saving?"Applying…":"Apply on next cycle"}</button></div>
      </section>
    </div>}
  </main>
}

function Metric({label,value,note,trend,accent=false}:{label:string;value:string;note:string;trend:string;accent?:boolean}){return <article className={`metric ${accent?"accent":""}`}><div><span>{label}</span><small>{trend}</small></div><strong>{value}</strong><p>{note}</p></article>}
function Gauge({label,value,min,max,suffix=""}:{label:string;value:number;min:number;max:number;suffix?:string}){const width=Math.max(0,Math.min(100,(value-min)/(max-min)*100));return <div className="gauge"><div><span>{label}</span><b>{value}{suffix}</b></div><i><em style={{width:`${width}%`}}/></i></div>}
function Source({name,detail,on}:{name:string;detail:string;on:boolean}){return <div className="source"><i className={on?"on":""}/><span><b>{name}</b><small>{detail}</small></span><em>{on?"READY":"IDLE"}</em></div>}
function RouteStep({number,title,text}:{number:string;title:string;text:string}){return <div className="routeStep"><span>{number}</span><b>{title}</b><small>{text}</small></div>}
function Toggle({checked,onChange,title,text,danger=false}:{checked:boolean;onChange:(next:boolean)=>void;title:string;text:string;danger?:boolean}){return <button type="button" className={`toggleRow ${danger?"danger":""}`} onClick={()=>onChange(!checked)} aria-pressed={checked}><span><b>{title}</b><small>{text}</small></span><i className={checked?"checked":""}><em/></i></button>}
function ControlRange({label,help,value,min,max,step,suffix="",onChange}:{label:string;help:string;value:number;min:number;max:number;step:number;suffix?:string;onChange:(value:number)=>void}){return <label className="controlRange"><span><b>{label}</b><strong>{value}{suffix}</strong></span><input type="range" value={value} min={min} max={max} step={step} onChange={e=>onChange(Number(e.target.value))}/><small>{help}</small></label>}
function SignalRow({signal}:{signal:Signal}){const route=signal.qualified?"DIRECTIONAL":signal.arbQualified?"ARB WATCH":"BLOCKED";return <details className="signalRow"><summary><span className={`side ${signal.side.toLowerCase()}`}>{signal.side}</span><span className="question"><b>{signal.question}</b><small>{route} · {signal.qualified?"all gates clear":signal.arbQualified?signal.arbGate:signal.gate}</small></span><ScoreMini label="CONF" value={signal.confidence}/><ScoreMini label="EDGE" value={signal.modelEdge}/><ScoreMini label="TIMING" value={signal.timingScore??0}/><span className={`routeBadge ${signal.qualified?"clear":signal.arbQualified?"watch":""}`}>{route}</span><span className="chevron">⌄</span></summary><div className="signalDetail"><Evidence label="Historical pattern" value={`${signal.historyScore.toFixed(1)}%`} note={`${signal.historyScope} · n=${signal.historySamples}`}/><Evidence label="Grounded news" value={signal.newsCount?`${signal.newsScore.toFixed(1)}%`:"N/A"} note={signal.newsCount?`${signal.newsCount} matching source${signal.newsCount===1?"":"s"}`:"Excluded: no matched evidence"}/><Evidence label="Prior / timing" value={`${(signal.marketPrior??50).toFixed(1)} / ${(signal.timingScore??0).toFixed(1)}`} note="Outcome prior / execution confirmation"/><Evidence label="Market capacity" value={`$${(signal.liquidity??0).toFixed(0)} / $${(signal.volume??0).toFixed(0)}`} note={`Liquidity / volume · ask ${(signal.entryAsk??0).toFixed(3)} · spread ${(signal.bookSpread??0).toFixed(1)}%`}/><Evidence label="Arb structure" value={`${signal.crossBookArb.toFixed(1)}%`} note={`${(signal.arbConfidence??0).toFixed(1)} confidence · paired execution locked`}/></div>{signal.newsSources.length>0&&<div className="sourceLinks">{signal.newsSources.map(source=><a href={source.url} target="_blank" rel="noreferrer" key={`${source.url}-${source.title}`}>{source.publisher} ↗</a>)}</div>}</details>}
function ScoreMini({label,value}:{label:string;value:number}){return <span className="scoreMini"><small>{label}</small><b>{value.toFixed(1)}</b></span>}
function Evidence({label,value,note}:{label:string;value:string;note:string}){return <div className="evidence"><span>{label}</span><b>{value}</b><small>{note}</small></div>}
function formatCycle(value?:string){if(!value)return "not connected";const date=new Date(value);return Number.isNaN(date.getTime())?value:date.toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",second:"2-digit"})}
