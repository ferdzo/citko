import {api,sendOps,ApiError} from './api';
import {applyChanges,db,deviceId,enqueue,getEntity,putEntity,type QueueRow} from './storage';
import type {Annotation,Book,Change,ReaderPreferences,SyncOp,SyncResult} from './types';
type Notify=()=>void;
let flushing:Promise<void>|null=null;
const listeners=new Set<Notify>();
export function subscribeSync(listener:Notify){listeners.add(listener);return ()=>{listeners.delete(listener)}}
export function announce(){listeners.forEach(fn=>fn())}
export async function queueOp(account:string,op:SyncOp){await enqueue(account,op);announce();try{await flush(account)}catch(error){if(!isConnectivityError(error))throw error}}
export async function startSync(account:string){
  await api('/devices',{method:'POST',body:JSON.stringify({id:deviceId,name:navigator.userAgent.includes('Mobile')?'Mobile browser':'This browser'})});
  await pull(account);await flush(account);
}
export async function pull(account:string){let cursor=(await db.accounts.get(account))?.cursor??0;let more=true;
  while(more){const result=await api<{changes:Change[];nextCursor:number;hasMore:boolean}>(`/sync/changes?after=${cursor}&limit=500`);
    await applyChanges(account,result.changes,result.nextCursor);
    const preference=result.changes.filter(change=>change.entity==='preferences').at(-1);
    if(preference)await db.accounts.update(account,{preferencesRevision:preference.revision});
    cursor=result.nextCursor;more=result.hasMore;
  }announce();
}
export async function flush(account:string):Promise<void>{
  if(flushing)return flushing;
  flushing=(async()=>{try{
    if(!navigator.onLine)return;
    const pending=await db.queue.where('[account+state]').equals([account,'pending']).sortBy('createdAt');
    for(let i=0;i<pending.length;i+=25){const chunk=pending.slice(i,i+25);
      const response=await sendOps(deviceId,chunk.map(item=>item.op));
      await db.transaction('rw',db.queue,db.entities,db.accounts,async()=>{
        for(const result of response.results){const row=chunk.find(item=>item.op.opId===result.opId);if(!row)continue;
          if(result.status==='applied')await db.queue.delete(row.id);
          else await db.queue.update(row.id,{state:result.status,result});
          if(result.current && result.status==='conflict'){
            const op=row.op;
            const kind=op.kind.startsWith('book.')?'book':op.kind.startsWith('annotation.')?'annotation':op.kind==='position.put'?'position':'preferences';
            const id='bookId' in op?op.bookId:'annotationId' in op?op.annotationId:'current';
            await putEntity(account,kind,id,result.current);
          }
        }
      });
    }
    await pull(account);
  }finally{flushing=null;announce()}})();return flushing;
}
export async function resolveConflict(account:string,row:QueueRow,choice:'mine'|'server'){
  if(choice==='mine'){
    const current=row.result?.current as {revision?:number}|undefined;
    if(current?.revision==null)throw new Error('Server revision unavailable. Refresh and try again.');
    const replacement={...row.op,opId:crypto.randomUUID(),baseRevision:current.revision} as SyncOp;
    await enqueue(account,replacement);
  }
  await db.queue.delete(row.id);announce();if(choice==='mine')await flush(account);
}
export async function queueBookPatch(account:string,book:Book,value:Partial<Book>){
  await putEntity(account,'book',book.id,{...book,...value});
  await queueOp(account,{opId:crypto.randomUUID(),kind:'book.patch',bookId:book.id,baseRevision:book.revision,value});
}
export async function queuePreferences(account:string,value:ReaderPreferences){
  const current=await getEntity<ReaderPreferences>(account,'preferences','current');
  const revision=(await db.accounts.get(account))?.preferencesRevision??null;
  if(current && JSON.stringify(current)===JSON.stringify(value))return;
  await putEntity(account,'preferences','current',value);
  await queueOp(account,{opId:crypto.randomUUID(),kind:'preferences.put',baseRevision:revision,value});
}
export async function queueAnnotation(account:string,value:Omit<Annotation,'revision'|'deleted'|'createdAt'>,baseRevision:number|null){
  await putEntity(account,'annotation',value.id,{...value,revision:baseRevision??0,deleted:false,createdAt:new Date().toISOString()});
  await queueOp(account,{opId:crypto.randomUUID(),kind:'annotation.put',annotationId:value.id,baseRevision,value:{bookId:value.bookId,renditionId:value.renditionId,type:value.type,locator:value.locator,anchor:value.anchor,quote:value.quote,note:value.note,color:value.color}});
}
export async function queueAnnotationDelete(account:string,value:Annotation){await putEntity(account,'annotation',value.id,{...value,deleted:true});await queueOp(account,{opId:crypto.randomUUID(),kind:'annotation.delete',annotationId:value.id,baseRevision:value.revision})}
export function isConnectivityError(error:unknown){return error instanceof ApiError && (error.status===0 || error.status>=500)}
export function errorText(error:unknown){return error instanceof Error?error.message:'Something went wrong. Please try again.'}
export function resultSummary(result?:SyncResult){return result?.message || result?.code || 'The server has a newer version.'}
