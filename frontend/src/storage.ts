import Dexie,{type Table} from 'dexie';
import type {Annotation,Book,Change,Collection,Position,ReaderPreferences,SyncOp,SyncResult} from './types';
export interface EntityRow {key:string;account:string;entity:string;id:string;value:unknown}
export interface QueueRow {id:string;account:string;op:SyncOp;state:'pending'|'conflict'|'rejected';result?:SyncResult;createdAt:number}
export interface DownloadRow {key:string;account:string;bookId:string;sha256:string;blob:Blob;size:number;savedAt:number}
export interface AccountRow {account:string;cursor:number;preferencesRevision:number|null}
export class CitkoDB extends Dexie {
  entities!:Table<EntityRow,string>;queue!:Table<QueueRow,string>;downloads!:Table<DownloadRow,string>;accounts!:Table<AccountRow,string>;
  constructor(name='citko-local'){super(name);this.version(1).stores({entities:'key,[account+entity],account',queue:'id,[account+state],account,createdAt',downloads:'key,account,bookId',accounts:'account'});}
}
export const db=new CitkoDB();
export const deviceId=(()=>{const key='citko-device-id';let value=localStorage.getItem(key);if(!value){value=crypto.randomUUID();localStorage.setItem(key,value)}return value})();
export function entityKey(account:string,entity:string,id:string){return `${account}:${entity}:${id}`}
export async function putEntity(account:string,entity:string,id:string,value:unknown){await db.entities.put({key:entityKey(account,entity,id),account,entity,id,value})}
export async function getEntity<T>(account:string,entity:string,id:string){return (await db.entities.get(entityKey(account,entity,id)))?.value as T|undefined}
export async function entities<T>(account:string,entity:string){return (await db.entities.where('[account+entity]').equals([account,entity]).toArray()).map(row=>row.value as T)}
export async function applyChanges(account:string,changes:Change[],cursor:number){
  await db.transaction('rw',db.entities,db.accounts,async()=>{
    for(const change of changes){
      if(change.value===null || ((change.entity==='book'||change.entity==='collection') && typeof change.value==='object' && change.value!==null && 'deleted' in change.value && change.value.deleted===true))
        await db.entities.delete(entityKey(account,change.entity,change.id));
      else await putEntity(account,change.entity,change.id,change.value);
    }
    const previous=await db.accounts.get(account);
    await db.accounts.put({account,cursor,preferencesRevision:previous?.preferencesRevision??null});
  });
}
export async function enqueue(account:string,op:SyncOp){
  const row:QueueRow={id:`${account}:${op.opId}`,account,op,state:'pending',createdAt:Date.now()};
  await db.queue.add(row);return row;
}
export async function pendingCount(account:string){return db.queue.where('[account+state]').equals([account,'pending']).count()}
export async function clearAccount(account:string){await db.transaction('rw',db.entities,db.queue,db.downloads,db.accounts,async()=>{
  await Promise.all([db.entities.where('account').equals(account).delete(),db.queue.where('account').equals(account).delete(),db.downloads.where('account').equals(account).delete(),db.accounts.delete(account)]);
})}
export async function cacheBook(account:string,book:Book){await putEntity(account,'book',book.id,book)}
export async function cacheAnnotations(account:string,values:Annotation[]){await db.transaction('rw',db.entities,async()=>{for(const value of values)await putEntity(account,'annotation',value.id,value)})}
export async function cacheCollections(account:string,values:Collection[]){await db.transaction('rw',db.entities,async()=>{for(const value of values)await putEntity(account,'collection',value.id,value)})}
export async function cachePosition(account:string,value:Position){await putEntity(account,'position',value.id,value)}
export async function cachePreferences(account:string,value:ReaderPreferences,revision:number){await db.transaction('rw',db.entities,db.accounts,async()=>{
  await putEntity(account,'preferences','current',value);const row=await db.accounts.get(account);await db.accounts.put({account,cursor:row?.cursor??0,preferencesRevision:revision});
})}
