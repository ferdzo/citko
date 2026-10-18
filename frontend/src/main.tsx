import React from 'react';
import {createRoot} from 'react-dom/client';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {BrowserRouter} from 'react-router-dom';
import App from './App';
import '@fontsource-variable/dm-sans';
import './style.css';

const client=new QueryClient({defaultOptions:{queries:{staleTime:30000,retry:(count,error)=>count<1 && !(error instanceof Error && error.message.includes('401'))}}});
createRoot(document.getElementById('root')!).render(<React.StrictMode><QueryClientProvider client={client}><BrowserRouter><App/></BrowserRouter></QueryClientProvider></React.StrictMode>);
if('serviceWorker' in navigator && import.meta.env.PROD)window.addEventListener('load',()=>navigator.serviceWorker.register('/sw.js').catch(()=>{}));
