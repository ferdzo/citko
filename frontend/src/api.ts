import type {AuthResponse, SyncOp, SyncResult} from './types';
export class ApiError extends Error { constructor(public status:number, public code:string, message:string, public details?:unknown){super(message)} }
let csrf = '';
export function setCsrf(value:string){csrf=value}
export async function api<T>(path:string, options:RequestInit = {}):Promise<T> {
  const method = options.method || 'GET';
  if (!['GET','HEAD'].includes(method.toUpperCase()) && !csrf && !path.startsWith('/auth/login') && !path.startsWith('/auth/setup')) {
    const token = await api<{csrfToken:string}>('/auth/csrf'); csrf=token.csrfToken;
  }
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type','application/json');
  if (!['GET','HEAD'].includes(method.toUpperCase()) && csrf) headers.set('X-CSRF-Token',csrf);
  let response:Response;
  try { response=await fetch('/api/v1'+path,{...options,headers,credentials:'same-origin'}); }
  catch { throw new ApiError(0,'offline','Could not reach the server. Your saved changes stay on this device.'); }
  if (!response.ok) {
    const data=await response.json().catch(()=>({})) as {error?:{code?:string;message?:string;details?:unknown}};
    if(response.status===401 && !path.startsWith('/auth/'))window.dispatchEvent(new Event('citko:session-expired'));
    throw new ApiError(response.status,data.error?.code || 'request_failed',data.error?.message || `Request failed (${response.status})`,data.error?.details);
  }
  if (response.status===204) return undefined as T;
  return response.json() as Promise<T>;
}
export const json=(value:unknown)=>JSON.stringify(value);
export async function login(email:string,password:string){const result=await api<AuthResponse>('/auth/login',{method:'POST',body:json({email,password})});setCsrf(result.csrfToken);return result}
export async function setup(email:string,password:string,name:string,token:string){const result=await api<AuthResponse>('/auth/setup',{method:'POST',headers:{'X-Setup-Token':token},body:json({email,password,name})});setCsrf(result.csrfToken);return result}
export async function sendOps(deviceId:string,ops:SyncOp[]){return api<{results:SyncResult[]}>('/sync/ops',{method:'POST',body:json({deviceId,ops})})}
export function uploadFile(file:File,onProgress:(fraction:number)=>void,token:string,path='/books'):Promise<{book:{id:string};jobId:string}> {
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();xhr.open('POST','/api/v1'+path);xhr.withCredentials=true;
    if(token)xhr.setRequestHeader('X-CSRF-Token',token);
    xhr.upload.onprogress=event=>{if(event.lengthComputable)onProgress(event.loaded/event.total)};
    xhr.onload=()=>{let data:unknown;try{data=JSON.parse(xhr.responseText)}catch{data={}}
      if(xhr.status>=200 && xhr.status<300)resolve(data as {book:{id:string};jobId:string});
      else {const error=(data as {error?:{code?:string;message?:string}}).error;reject(new ApiError(xhr.status,error?.code||'upload_failed',error?.message||`Upload failed (${xhr.status})`))}
    };xhr.onerror=()=>reject(new ApiError(0,'offline','Connection lost during upload. Please try again.'));
    const body=new FormData();body.append('file',file);xhr.send(body);
  });
}
export async function uploadCsrf(){if(!csrf){csrf=(await api<{csrfToken:string}>('/auth/csrf')).csrfToken}return csrf}
export async function downloadAttachment(path:string,filename:string){
  const response=await fetch('/api/v1'+path,{credentials:'same-origin'});
  if(!response.ok)throw new Error(`Download failed (${response.status})`);
  const blob=await response.blob();const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download=filename;document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
}
