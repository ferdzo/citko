import 'fake-indexeddb/auto';
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import {defaultPreferences} from '../types';
import {db,enqueue,getEntity,putEntity} from '../storage';
import {flush,queuePreferences,resolveConflict} from '../sync';
import {api,sendOps} from '../api';

vi.mock('../api',()=>({
 api:vi.fn(async()=>({changes:[],nextCursor:0,hasMore:false})),
 sendOps:vi.fn(async(_device:string,ops:{opId:string}[])=>({results:ops.map(op=>({opId:op.opId,status:'applied',revision:10}))})),
 ApiError:class ApiError extends Error{status=0},
}));
beforeEach(async()=>{await db.delete();await db.open();vi.clearAllMocks()});
afterEach(async()=>{await db.delete()});
describe('durable sync queue',()=>{
 it('persists edits before sending and retries using the same idempotency key',async()=>{
  const op={opId:'same-id-after-retry',kind:'book.delete' as const,bookId:'book',baseRevision:4};
  await enqueue('account-a',op);
  expect((await db.queue.get('account-a:same-id-after-retry'))?.op.opId).toBe(op.opId);
  await flush('account-a');
  expect(sendOps).toHaveBeenCalledWith(expect.any(String),[op]);
  expect(await db.queue.count()).toBe(0);
  expect(api).toHaveBeenCalled();
 });
 it('retains conflicting original and creates a new ID against server revision',async()=>{
  const op={opId:'original',kind:'book.patch' as const,bookId:'book',baseRevision:3,value:{title:'My title'}};
  const row=await enqueue('account-a',op);
  await db.queue.update(row.id,{state:'conflict',result:{opId:'original',status:'conflict',current:{revision:8,title:'Their title'}}});
  const conflict=(await db.queue.get(row.id))!;
  await resolveConflict('account-a',conflict,'mine');
  expect(sendOps).toHaveBeenCalledWith(expect.any(String),[expect.objectContaining({baseRevision:8,kind:'book.patch',value:{title:'My title'}})]);
  const resent=vi.mocked(sendOps).mock.calls[0][1][0];
  expect(resent.opId).not.toBe(op.opId);
 });
 it('saves and replaces a preference snapshot locally',async()=>{
  await putEntity('account-a','preferences','current',defaultPreferences);
  await db.accounts.put({account:'account-a',cursor:2,preferencesRevision:5});
  await queuePreferences('account-a',{...defaultPreferences,goalMinutes:35});
  expect((await getEntity<typeof defaultPreferences>('account-a','preferences','current'))?.goalMinutes).toBe(35);
  expect(sendOps).toHaveBeenCalledWith(expect.any(String),[expect.objectContaining({kind:'preferences.put',baseRevision:5,value:expect.objectContaining({goalMinutes:35})})]);
 });
});
