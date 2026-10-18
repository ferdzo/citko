import {createContext,useContext,useEffect,useState} from 'react';
import {Link,NavLink,Route,Routes,useLocation,useNavigate} from 'react-router-dom';
import {useQueryClient} from '@tanstack/react-query';
import {BookOpen,ChartNoAxesCombined,CloudOff,CloudUpload,Highlighter,House,Images,Layers3,LibraryBig,LogOut,Menu,Plus,Settings2,Wifi,X} from 'lucide-react';
import type {User} from './types';
import {api,login,setCsrf,setup} from './api';
import {clearAccount,db,pendingCount} from './storage';
import {errorText,flush,pull,startSync,subscribeSync} from './sync';
import {Home,Library,BookDetails,ReaderRoute,Annotations,Statistics,Settings,Imports,Collections,Upload} from './screens';
import {Photos} from './photos';

interface Session {user:User|null;setUser:(user:User|null)=>void;offline:boolean;refresh:()=>Promise<void>}
const SessionContext=createContext<Session|null>(null);
export function useSession(){const value=useContext(SessionContext);if(!value)throw new Error('Session missing');return value}

export default function App(){
  const [user,setUserState]=useState<User|null>(null),[checking,setChecking]=useState(true),[offline,setOffline]=useState(!navigator.onLine);
  const setUser=(next:User|null)=>{setUserState(next);if(next)localStorage.setItem('citko-last-user',JSON.stringify(next))};
  const refresh=async()=>{try{const session=await api<{user:User;csrfToken:string}>('/auth/me');setCsrf(session.csrfToken);setUser(session.user);setOffline(false)}catch(error){
    if(!navigator.onLine || (error instanceof Error&&error.message.includes('reach the server'))){const cached=localStorage.getItem('citko-last-user');if(cached){try{setUserState(JSON.parse(cached) as User);setOffline(true)}catch{setUserState(null)}}}
    else setUserState(null);
  }finally{setChecking(false)}};
  useEffect(()=>{void refresh();const online=()=>{setOffline(false);void refresh()},off=()=>setOffline(true),expired=()=>{setUserState(null);setCsrf('')};window.addEventListener('online',online);window.addEventListener('offline',off);window.addEventListener('citko:session-expired',expired);return()=>{window.removeEventListener('online',online);window.removeEventListener('offline',off);window.removeEventListener('citko:session-expired',expired)}},[]);
  useEffect(()=>{if(!user||offline)return;void startSync(user.id).catch(()=>{});const timer=setInterval(()=>{void flush(user.id).catch(()=>{})},30000);const visible=()=>{if(document.visibilityState==='visible'){void pull(user.id).catch(()=>{});void flush(user.id).catch(()=>{})}};document.addEventListener('visibilitychange',visible);return()=>{clearInterval(timer);document.removeEventListener('visibilitychange',visible)}},[user?.id,offline]);
  if(checking)return <div className="min-h-screen flex items-center justify-center bg-white"><span className="font-semibold tracking-tight text-xl">citko</span></div>;
  return <SessionContext.Provider value={{user,setUser,offline,refresh}}>{user?<Shell/>:<Auth/>}</SessionContext.Provider>;
}

function Auth(){
  const {setUser}=useSession(),navigate=useNavigate();
  const [mode,setMode]=useState<'login'|'setup'>('login'),[setupRequired,setSetupRequired]=useState(false),[email,setEmail]=useState(''),[password,setPassword]=useState(''),[name,setName]=useState(''),[token,setToken]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  useEffect(()=>{api<{required:boolean}>('/auth/setup').then(result=>{setSetupRequired(result.required);setMode(result.required?'setup':'login')}).catch(()=>{})},[]);
  const submit=async(event:React.FormEvent)=>{event.preventDefault();setBusy(true);setError('');try{const result=mode==='setup'?await setup(email,password,name,token):await login(email,password);setUser(result.user);navigate('/')}catch(err){setError(errorText(err))}finally{setBusy(false)}};
  return <div className="min-h-screen bg-white flex flex-col">
    <header className="h-16 border-b border-line flex items-center px-6 sm:px-10"><Link to="/" className="text-lg font-bold tracking-[-.06em]">citko<span className="text-muted">.</span></Link></header>
    <main className="flex-1 grid place-items-center px-5 py-12"><div className="w-full max-w-[390px]">
      <div className="w-11 h-11 border border-line rounded-lg grid place-items-center mb-7"><BookOpen size={20} strokeWidth={1.7}/></div>
      <h1 className="text-[1.8rem] font-semibold tracking-tight leading-tight">{mode==='setup'?'Create your account':'Sign in to Citko'}</h1>
      <p className="text-sm text-muted mt-2 mb-8">{mode==='setup'?'Set up your library to get started.':'Access your books and reading notes.'}</p>
      <form onSubmit={submit} className="space-y-4">{mode==='setup'&&<><div><label className="label" htmlFor="name">Your name</label><input id="name" className="field" required value={name} onChange={e=>setName(e.target.value)}/></div><div><label className="label" htmlFor="token">Setup token</label><input id="token" className="field" required value={token} onChange={e=>setToken(e.target.value)} autoComplete="off"/><p className="text-xs text-muted mt-1.5">Find this in the server setup instructions.</p></div></>}
        <div><label className="label" htmlFor="email">Email address</label><input id="email" type="email" autoComplete="email" className="field" required value={email} onChange={e=>setEmail(e.target.value)}/></div>
        <div><label className="label" htmlFor="password">Password</label><input id="password" type="password" autoComplete={mode==='setup'?'new-password':'current-password'} minLength={mode==='setup'?12:undefined} className="field" required value={password} onChange={e=>setPassword(e.target.value)}/></div>
        {error&&<p role="alert" className="notice notice-error">{error}</p>}
        <button disabled={busy} className="btn btn-primary w-full !min-h-11">{busy?'Please wait…':mode==='setup'?'Create reading room':'Sign in'}</button>
      </form>{setupRequired&&<p className="text-xs text-muted mt-5">First-time setup requires the server’s setup token.</p>}
    </div></main><footer className="px-6 py-5 text-xs text-muted text-center">Your library, on your devices.</footer>
  </div>;
}

const nav=[
  {to:'/',name:'Home',icon:House},{to:'/library',name:'Library',icon:LibraryBig},
  {to:'/photos',name:'Photos',icon:Images},
  {to:'/annotations',name:'Notes & highlights',icon:Highlighter},{to:'/collections',name:'Collections',icon:Layers3},
  {to:'/stats',name:'Statistics',icon:ChartNoAxesCombined},{to:'/imports',name:'Imports',icon:CloudUpload},
  {to:'/settings',name:'Settings',icon:Settings2}
];
function Shell(){
  const {user,offline,setUser}=useSession(),navigate=useNavigate(),location=useLocation(),client=useQueryClient();
  const [menu,setMenu]=useState(false),[count,setCount]=useState(0),[logoutError,setLogoutError]=useState('');
  useEffect(()=>{if(!user)return;const update=()=>{void pendingCount(user.id).then(setCount)};update();return subscribeSync(update)},[user?.id]);
  useEffect(()=>setMenu(false),[location.pathname]);
  const logout=async()=>{if(!user)return;const outstanding=await db.queue.where('account').equals(user.id).count();if(outstanding&&!window.confirm(`${outstanding} change${outstanding===1?' is':'s are'} still saved only on this device. Signing out clears these changes and offline downloads. Sign out anyway?`))return;
    if(offline){setLogoutError('Connect to the server to sign out. Your saved changes are still here.');return}
    try{await api('/auth/logout',{method:'POST'});await clearAccount(user.id);localStorage.removeItem('citko-last-user');setCsrf('');client.clear();setUser(null);navigate('/')}catch(error){setLogoutError(errorText(error))}};
  if(location.pathname.startsWith('/read/'))return <Routes><Route path="/read/:id" element={<ReaderRoute/>}/></Routes>;
  return <div className="min-h-screen bg-paper text-ink">
    <aside className={`citko-sidebar fixed z-50 inset-y-0 left-0 w-[236px] bg-white border-r border-line flex flex-col transition-transform duration-200 lg:translate-x-0 ${menu?'translate-x-0':'-translate-x-full'}`}>
      <div className="px-5 h-[68px] flex items-center justify-between shrink-0"><Link to="/" className="text-xl font-bold tracking-[-.06em]">citko<span className="text-muted">.</span></Link><button aria-label="Close menu" className="lg:hidden p-2 text-muted" onClick={()=>setMenu(false)}><X size={20}/></button></div>
      <nav aria-label="Main navigation" className="px-3 pt-5 space-y-0.5">{nav.map(item=><NavLink end={item.to==='/'} key={item.to} to={item.to} className={({isActive})=>`flex items-center gap-3 rounded-md px-3 py-2.5 text-[.86rem] font-medium transition-colors ${isActive?'bg-[#f0f0f0] text-[#111]':'text-[#646464] hover:bg-[#f6f6f6] hover:text-[#171717]'}`}><item.icon size={18} strokeWidth={1.8}/>{item.name}</NavLink>)}</nav>
      <div className="mt-auto px-4 pb-5"><Link to="/upload" className="btn btn-primary w-full mb-5"><Plus size={16}/> Add books</Link><div className="border-t border-line pt-4 flex items-center gap-2.5"><div className="w-8 h-8 rounded-md bg-[#eeeeee] grid place-items-center text-xs font-semibold shrink-0">{user?.name?.[0]?.toUpperCase()}</div><div className="min-w-0 flex-1"><p className="text-xs font-semibold truncate">{user?.name}</p><p className="text-[11px] text-muted truncate mt-0.5">{user?.email}</p></div><button title="Sign out" aria-label="Sign out" onClick={()=>void logout()} className="p-1.5 text-muted hover:text-ink rounded hover:bg-[#f4f4f4]"><LogOut size={17}/></button></div></div>
    </aside>
    {menu&&<button aria-label="Close menu" className="fixed inset-0 z-40 bg-black/35 lg:hidden" onClick={()=>setMenu(false)}/>}
    <div className="lg:pl-[236px] min-h-screen flex flex-col">
      <header className="citko-header px-5 sm:px-8 lg:px-9 flex items-center justify-between border-b border-line bg-white sticky top-0 z-30"><div className="flex items-center gap-3"><button className="lg:hidden p-1" aria-label="Open menu" onClick={()=>setMenu(true)}><Menu size={21}/></button><Link to="/" className="lg:hidden text-lg font-bold tracking-[-.06em]">citko.</Link><span className="hidden lg:block text-sm text-muted">{nav.find(item=>item.to===location.pathname)?.name||'Library'}</span></div><div className="flex items-center gap-3 sm:gap-5"><span title={offline?'Offline':count?'Changes waiting to sync':'Connected'} className="inline-flex gap-1.5 items-center text-xs text-muted">{offline||count?<CloudOff size={15}/>:<Wifi size={15}/>}<span className="hidden sm:inline">{offline?'Offline':count?`${count} pending`:'Connected'}</span></span><Link to="/upload" className="hidden sm:inline-flex btn btn-quiet !min-h-9 !py-1.5"><Plus size={16}/> Add books</Link></div></header>
      {(offline||logoutError)&&<div role="status" className="bg-[#f4f4f4] border-b border-line px-5 py-2.5 text-center text-xs text-[#555]">{logoutError||'Offline. Saved books and notes are available; changes will sync when you reconnect.'}</div>}
      <main className="w-full max-w-[1440px] mx-auto px-5 sm:px-8 lg:px-10 py-7 sm:py-9 flex-1"><Routes><Route path="/" element={<Home/>}/><Route path="/library" element={<Library/>}/><Route path="/photos" element={<Photos/>}/><Route path="/books/:id" element={<BookDetails/>}/><Route path="/annotations" element={<Annotations/>}/><Route path="/collections" element={<Collections/>}/><Route path="/stats" element={<Statistics/>}/><Route path="/imports" element={<Imports/>}/><Route path="/settings" element={<Settings/>}/><Route path="/upload" element={<Upload/>}/><Route path="*" element={<div className="text-center py-24"><h1 className="text-2xl font-semibold">Page not found</h1><Link className="btn btn-primary mt-6" to="/">Back home</Link></div>}/></Routes></main>
      <footer className="citko-footer px-5 sm:px-10 py-6 border-t border-line text-xs text-muted flex justify-between bg-white"><span>Citko</span><Link to="/settings" className="hover:text-ink">Settings</Link></footer>
    </div>
  </div>;
}
