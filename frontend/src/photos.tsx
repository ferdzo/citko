import {useEffect,useRef,useState,type ChangeEvent,type DragEvent} from 'react';
import {useQuery,useQueryClient} from '@tanstack/react-query';
import {ArrowLeft,ArrowRight,Check,Heart,Images,LoaderCircle,MapPin,Plus,Trash2,UploadCloud,X} from 'lucide-react';
import type {Page,Photo} from './types';
import {api,uploadCsrf,uploadFile} from './api';
import {errorText} from './sync';
import {useSession} from './App';
import {Empty,ErrorMessage,Heading} from './screens';

type QueueItem={file:File;progress:number;status:'waiting'|'uploading'|'done'|'error';message?:string};

const ACCEPTED='image/jpeg,image/png,image/webp,image/gif,image/tiff,image/bmp';

/** Only rows that actually carry a value are returned: most photos have several empty tags and
 *  empty rows would read as missing data rather than absent metadata. */
/** Sizes as displayed: orientations 5-8 are turned a quarter turn, so the raw pixel dimensions
 *  read the wrong way round for a photo the user is looking at upright. */
function dimensions(photo:Photo):string|null{
  if(!photo.width||!photo.height)return null;
  const turned=photo.orientation!==null&&photo.orientation>=5;
  return turned?`${photo.height} × ${photo.width}`:`${photo.width} × ${photo.height}`;
}

function facts(photo:Photo):[string,string][]{
  const rows:[string,string|null][]=[
    ['Taken',photo.takenAt?new Date(photo.takenAt).toLocaleString():null],
    ['Camera',photo.camera],
    ['Lens',photo.lens],
    ['ISO',photo.iso?`ISO ${photo.iso}`:null],
    ['Aperture',photo.aperture],
    ['Shutter',photo.shutter],
    ['Focal length',photo.focalLength],
    ['Dimensions',dimensions(photo)],
    ['Location',photo.gpsLat!==null&&photo.gpsLon!==null?`${photo.gpsLat.toFixed(4)}, ${photo.gpsLon.toFixed(4)}`:null],
    ['Size',`${Math.max(1,Math.round(photo.size/1024))} KB`],
    ['File',photo.fileName],
  ];
  return rows.filter((row):row is [string,string]=>Boolean(row[1]));
}

export function Photos(){
  const {offline}=useSession(),client=useQueryClient();
  const [page,setPage]=useState(1),[sort,setSort]=useState<'taken'|'added'>('taken'),[favorites,setFavorites]=useState(false);
  const [error,setError]=useState(''),[hover,setHover]=useState(false),[busy,setBusy]=useState(false);
  const [queue,setQueue]=useState<QueueItem[]>([]);
  const [open,setOpen]=useState<number|null>(null),[caption,setCaption]=useState(''),[loaded,setLoaded]=useState(false);
  const input=useRef<HTMLInputElement>(null),closeButton=useRef<HTMLButtonElement>(null),previouslyFocused=useRef<Element|null>(null);
  const pageSize=60,isOpen=open!==null;

  const query=useQuery({queryKey:['photos',page,sort,favorites],enabled:!offline,queryFn:()=>api<Page<Photo>>(`/photos?page=${page}&pageSize=${pageSize}&sort=${sort}${favorites?'&favorite=true':''}`)});
  const items=query.data?.items??[],current=open===null?null:items[open]??null,pending=queue.filter(item=>item.status!=='done');
  const uploading=queue.some(item=>item.status==='uploading');

  useEffect(()=>{setCaption(current?.caption??'')},[current?.id,current?.caption]);
  useEffect(()=>{setLoaded(false)},[current?.id]);

  // Focus and scroll locking belong to opening the lightbox, not to moving between photos in it.
  useEffect(()=>{
    if(!isOpen)return;
    previouslyFocused.current=document.activeElement;
    closeButton.current?.focus();
    document.body.style.overflow='hidden';
    return ()=>{document.body.style.overflow='';(previouslyFocused.current as HTMLElement|null)?.focus?.()};
  },[isOpen]);

  useEffect(()=>{
    if(!isOpen)return;
    const key=(event:KeyboardEvent)=>{
      if(event.key==='Escape')setOpen(null);
      else if(event.key==='ArrowRight')setOpen(index=>index===null?index:Math.min(index+1,items.length-1));
      else if(event.key==='ArrowLeft')setOpen(index=>index===null?index:Math.max(index-1,0));
    };
    document.addEventListener('keydown',key);
    return ()=>document.removeEventListener('keydown',key);
  },[isOpen,items.length]);

  const add=(files:FileList|File[])=>{
    // Materialise the list eagerly: a FileList is live, and clearing the input's value empties it
    // before a deferred state updater would read it, silently dropping the selection.
    const picked=Array.from(files);
    if(!picked.length)return;
    setQueue(list=>[...list,...picked.map(file=>({file,progress:0,status:'waiting' as const}))]);
  };
  const drop=(event:DragEvent)=>{event.preventDefault();setHover(false);if(event.dataTransfer.files.length)add(event.dataTransfer.files)};
  const choose=(event:ChangeEvent<HTMLInputElement>)=>{if(event.target.files?.length)add(event.target.files);event.target.value=''};

  const upload=async()=>{
    setError('');
    // One request in flight at a time: the endpoint takes a single file.
    for(let index=0;index<queue.length;index++){
      if(queue[index].status==='done')continue;
      const file=queue[index].file;
      setQueue(list=>list.map((item,at)=>at===index?{...item,status:'uploading',message:undefined}:item));
      try{
        const token=await uploadCsrf();
        await uploadFile(file,value=>setQueue(list=>list.map((item,at)=>at===index?{...item,progress:Math.round(value*100)}:item)),token,'/photos');
        setQueue(list=>list.map((item,at)=>at===index?{...item,status:'done',progress:100}:item));
      }catch(err){
        setQueue(list=>list.map((item,at)=>at===index?{...item,status:'error',message:errorText(err)}:item));
      }
    }
    // Uploaded photos are in the grid now, so they leave the queue; failures stay to be retried.
    setQueue(list=>list.filter(item=>item.status!=='done'));
    await client.invalidateQueries({queryKey:['photos']});
  };

  const patch=async(id:string,body:Record<string,unknown>)=>{
    setBusy(true);setError('');
    try{await api(`/photos/${id}`,{method:'PATCH',body:JSON.stringify(body)});await client.invalidateQueries({queryKey:['photos']})}
    catch(err){setError(errorText(err))}
    finally{setBusy(false)}
  };
  const remove=async(photo:Photo)=>{
    if(!window.confirm('Delete this photo? It disappears from your library.'))return;
    setBusy(true);setError('');
    try{await api(`/photos/${photo.id}`,{method:'DELETE'});setOpen(null);await client.invalidateQueries({queryKey:['photos']})}
    catch(err){setError(errorText(err))}
    finally{setBusy(false)}
  };

  const controls=<div className="flex flex-wrap items-center gap-2">
    <button className={favorites?'btn btn-primary !py-2':'btn btn-quiet !py-2'} aria-pressed={favorites} onClick={()=>{setFavorites(!favorites);setPage(1)}}><Heart size={15} fill={favorites?'currentColor':'none'}/> Favorites</button>
    <button className="btn btn-quiet !py-2" onClick={()=>{setSort(sort==='taken'?'added':'taken');setPage(1)}}>{sort==='taken'?'Date taken':'Recently added'}</button>
  </div>;

  return <>
    <Heading eyebrow="" title="Photos" description="Your own photographs, with the details your camera recorded." action={<button className="btn btn-primary" disabled={offline} onClick={()=>input.current?.click()}><UploadCloud size={16}/> Add photos</button>}/>

    <input ref={input} type="file" accept={ACCEPTED} multiple className="hidden" onChange={choose}/>

    <div onDragOver={event=>{event.preventDefault();setHover(true)}} onDragLeave={()=>setHover(false)} onDrop={drop}
      className={`rounded-xl border border-dashed px-5 py-7 text-center mb-6 transition-colors ${hover?'border-[#9a9a9a] bg-[#f6f6f6]':'border-[#dcdcdc] bg-white'}`}>
      <p className="text-sm text-muted">Drop photos here, or <button className="underline font-semibold text-forest" onClick={()=>input.current?.click()}>choose files</button>. JPEG, PNG, WebP, GIF, TIFF and BMP.</p>
      {queue.length>0&&<div className="mt-5 space-y-2 text-left">
        {queue.map((item,index)=><div key={`${item.file.name}-${index}`} className="flex items-center gap-3">
          <span className="flex-1 min-w-0 truncate text-xs text-[#555]">{item.file.name}</span>
          {item.status==='error'?<span className="text-xs text-[#9b2626] shrink-0 max-w-[50%] truncate">{item.message}</span>
            :item.status==='done'?<Check size={15} className="text-forest shrink-0"/>
            :<span className="w-28 shrink-0 h-1 rounded bg-[#e5e5e5] overflow-hidden"><span className="block h-full bg-[#171717]" style={{width:`${item.progress}%`}}/></span>}
        </div>)}
        {pending.length>0&&<button className="btn btn-primary mt-2" disabled={uploading||offline} onClick={()=>void upload()}>{uploading?<><LoaderCircle size={15} className="animate-spin"/> Uploading…</>:`Upload ${pending.length} photo${pending.length===1?'':'s'}`}</button>}
      </div>}
    </div>

    {error&&<p className="notice notice-error mb-5">{error}</p>}
    {controls&&<div className="mb-5">{controls}</div>}

    {offline?<p className="notice">Photos are stored on the server, so they need a connection. Your books and notes still work offline.</p>
      :query.isLoading?<div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-3">{Array.from({length:12},(_,index)=><div key={index} className="skeleton aspect-square"/>)}</div>
      :query.isError?<ErrorMessage error={query.error} onRetry={()=>void query.refetch()}/>
      :items.length?<>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-3">
          {items.map((photo,index)=><button key={photo.id} onClick={()=>setOpen(index)} className="group relative aspect-square overflow-hidden rounded-md border border-line bg-[#f1f1f1]" aria-label={`Open ${photo.caption||photo.fileName}`}>
            <img loading="lazy" src={photo.thumbnailUrl} alt={photo.caption||photo.fileName} className="w-full h-full object-cover transition-transform duration-300 group-hover:scale-[1.03]"/>
            {photo.favorite&&<span className="absolute top-2 right-2 w-6 h-6 rounded-full bg-white/90 grid place-items-center text-forest"><Heart size={13} fill="currentColor"/></span>}
          </button>)}
        </div>
        {query.data&&query.data.total>pageSize&&<div className="flex justify-center gap-3 mt-7"><button className="btn btn-quiet" disabled={page===1} onClick={()=>setPage(page-1)}>Previous</button><button className="btn btn-quiet" disabled={page*pageSize>=query.data.total} onClick={()=>setPage(page+1)}>Next</button></div>}
      </>
      :<Empty icon={<Images size={27} strokeWidth={1.5} className="mx-auto text-muted mb-4"/>} title={favorites?'No favorites yet':'No photos yet'} description={favorites?'Photos you mark as favorites collect here.':'Add your own photographs to keep them alongside your library.'} action={<button className="btn btn-primary" onClick={()=>input.current?.click()}><Plus size={16}/> Add photos</button>}/>}

    {current&&<div role="dialog" aria-modal="true" aria-label={`Photo ${open!+1} of ${items.length}`} className="fixed inset-0 z-50 bg-black/85 overflow-y-auto flex flex-col lg:flex-row">
      <div className="relative flex-1 min-h-[60vh] lg:min-h-0 flex items-center justify-center p-4 lg:p-8" onClick={()=>setOpen(null)}>
        {/* The thumbnail is already in the browser cache from the grid, so it stands in while the
            full-size photo is still in flight instead of leaving an empty frame. */}
        <img src={current.thumbnailUrl} alt="" aria-hidden="true" onClick={event=>event.stopPropagation()} className="max-h-[60vh] lg:max-h-[86vh] max-w-full object-contain blur-[2px]"/>
        <img src={current.fileUrl} alt={current.caption||current.fileName} onLoad={()=>setLoaded(true)} className={`absolute max-h-[60vh] lg:max-h-[86vh] max-w-full object-contain transition-opacity duration-300 ${loaded?'opacity-100':'opacity-0'}`} onClick={event=>event.stopPropagation()}/>
        <button aria-label="Previous photo" disabled={open===0} onClick={event=>{event.stopPropagation();setOpen(Math.max(0,(open??0)-1))}} className="absolute left-3 top-1/2 -translate-y-1/2 w-10 h-10 rounded-full bg-white/85 grid place-items-center text-ink disabled:opacity-30"><ArrowLeft size={18}/></button>
        <button aria-label="Next photo" disabled={open===items.length-1} onClick={event=>{event.stopPropagation();setOpen(Math.min(items.length-1,(open??0)+1))}} className="absolute right-3 top-1/2 -translate-y-1/2 w-10 h-10 rounded-full bg-white/85 grid place-items-center text-ink disabled:opacity-30"><ArrowRight size={18}/></button>
      </div>
      <aside className="w-full lg:w-[360px] shrink-0 bg-white lg:h-screen lg:overflow-y-auto p-6" onClick={event=>event.stopPropagation()}>
        <div className="flex items-center justify-between mb-5">
          <p className="eyebrow">{open!+1} of {items.length}</p>
          <div className="flex items-center gap-1">
            <button aria-label={current.favorite?'Remove from favorites':'Add to favorites'} disabled={busy||offline} onClick={()=>void patch(current.id,{favorite:!current.favorite})} className="p-2 rounded hover:bg-[#f4f4f4]"><Heart size={17} className={current.favorite?'text-forest':'text-muted'} fill={current.favorite?'currentColor':'none'}/></button>
            <button aria-label="Delete photo" disabled={busy||offline} onClick={()=>void remove(current)} className="p-2 rounded text-muted hover:text-[#9b2626] hover:bg-[#f4f4f4]"><Trash2 size={17}/></button>
            <button ref={closeButton} aria-label="Close photo" onClick={()=>setOpen(null)} className="p-2 rounded text-muted hover:text-ink hover:bg-[#f4f4f4]"><X size={18}/></button>
          </div>
        </div>
        <label className="label" htmlFor="photo-caption">Caption</label>
        <input id="photo-caption" className="field" value={caption} placeholder="Add a caption" disabled={busy||offline} onChange={event=>setCaption(event.target.value)} onBlur={()=>{if(caption!==(current.caption??''))void patch(current.id,{caption})}} onKeyDown={event=>{if(event.key==='Enter'){event.preventDefault();(event.target as HTMLInputElement).blur()}}}/>
        <dl className="space-y-3 text-sm mt-6">
          {facts(current).map(([label,value])=><div key={label} className="flex gap-4 justify-between"><dt className="text-muted shrink-0">{label}</dt><dd className="text-right break-words">{value}</dd></div>)}
        </dl>
        {current.gpsLat!==null&&current.gpsLon!==null&&<a href={`https://www.openstreetmap.org/?mlat=${current.gpsLat}&mlon=${current.gpsLon}#map=15/${current.gpsLat}/${current.gpsLon}`} target="_blank" rel="noreferrer" className="btn btn-quiet w-full mt-7"><MapPin size={15}/> Open location map</a>}
      </aside>
    </div>}
  </>;
}
