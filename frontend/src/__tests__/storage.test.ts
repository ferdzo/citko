import 'fake-indexeddb/auto';
import {afterEach,beforeEach,describe,expect,it} from 'vitest';
import {CitkoDB,entityKey} from '../storage';
import type {Change} from '../types';

let database:CitkoDB;
beforeEach(()=>{database=new CitkoDB(`citko-test-${crypto.randomUUID()}`)});
afterEach(async()=>{await database.delete()});
describe('account-scoped offline snapshots',()=>{
 it('keeps the same server entity in separate accounts',async()=>{
  await database.entities.put({key:entityKey('alice','book','one'),account:'alice',entity:'book',id:'one',value:{title:'Alice copy'}});
  await database.entities.put({key:entityKey('bob','book','one'),account:'bob',entity:'book',id:'one',value:{title:'Bob copy'}});
  expect((await database.entities.get(entityKey('alice','book','one')))?.value).toEqual({title:'Alice copy'});
  expect((await database.entities.get(entityKey('bob','book','one')))?.value).toEqual({title:'Bob copy'});
 });
 it('keeps sync cursor and snapshots in one transaction',async()=>{
  const changes:Change[]=[{revision:12,entity:'book',id:'id-1',value:{title:'A real book'}}];
  await database.transaction('rw',database.entities,database.accounts,async()=>{
   for(const item of changes)await database.entities.put({key:entityKey('alice',item.entity,item.id),account:'alice',entity:item.entity,id:item.id,value:item.value});
   await database.accounts.put({account:'alice',cursor:12,preferencesRevision:null});
  });
  expect((await database.accounts.get('alice'))?.cursor).toBe(12);
  expect((await database.entities.get(entityKey('alice','book','id-1')))?.value).toEqual({title:'A real book'});
 });
 it('never reuses a queued operation ID and preserves it across retries',async()=>{
  const op={opId:crypto.randomUUID(),kind:'book.delete' as const,bookId:'book',baseRevision:5};
  await database.queue.add({id:`alice:${op.opId}`,account:'alice',op,state:'pending',createdAt:1});
  const saved=await database.queue.get(`alice:${op.opId}`);
  expect(saved?.op).toEqual(op);
  await expect(database.queue.add({id:`alice:${op.opId}`,account:'alice',op,state:'pending',createdAt:2})).rejects.toThrow();
  expect(await database.queue.count()).toBe(1);
 });
});
it('does not lose queued edits when a snapshot arrives',async()=>{
 const op={opId:'stable',kind:'book.delete' as const,bookId:'book',baseRevision:5};
 await database.queue.add({id:'alice:stable',account:'alice',op,state:'pending',createdAt:1});
 await database.transaction('rw',database.entities,database.accounts,async()=>{
  await database.entities.put({key:entityKey('alice','book','book'),account:'alice',entity:'book',id:'book',value:{revision:6}});
  await database.accounts.put({account:'alice',cursor:6,preferencesRevision:null});
 });
 expect((await database.queue.get('alice:stable'))?.op.opId).toBe('stable');
});
