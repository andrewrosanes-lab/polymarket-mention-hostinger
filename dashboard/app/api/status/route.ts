import { readFile } from "node:fs/promises";

export const dynamic = "force-dynamic";
export async function GET(){
  const headers={"Cache-Control":"no-store"};
  const statusFile=process.env.MENTION_BOT_STATUS_FILE;
  const endpoint=process.env.MENTION_BOT_STATUS_URL;
  try{
    if(statusFile){
      const status=JSON.parse(await readFile(statusFile,"utf8"));
      const cycleTime=Date.parse(String(status.lastCycle||""));
      const stale=!Number.isFinite(cycleTime)||Date.now()-cycleTime>10*60*1000;
      return Response.json({...status,connected:!stale,stale},{headers});
    }
    if(endpoint){
      const response=await fetch(endpoint,{cache:"no-store",signal:AbortSignal.timeout(5000)});
      if(!response.ok)throw new Error(`status endpoint returned ${response.status}`);
      return Response.json({...await response.json(),connected:true},{headers});
    }
  }catch{
    return Response.json({connected:false,mode:"LIVE"},{status:503,headers});
  }
  return Response.json({connected:false,mode:"LIVE"},{headers});
}
