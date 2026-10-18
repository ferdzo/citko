import {readdir,writeFile} from 'node:fs/promises';
import {join,relative} from 'node:path';
const root='dist';
async function list(dir){const entries=await readdir(dir,{withFileTypes:true});return (await Promise.all(entries.map(async entry=>entry.isDirectory()?list(join(dir,entry.name)):[`/${relative(root,join(dir,entry.name)).replaceAll('\\','/')}`]))).flat()}
const assets=(await list(join(root,'assets'))).filter(path=>!path.endsWith('.map'));
await writeFile(join(root,'asset-manifest.json'),JSON.stringify({assets}));
