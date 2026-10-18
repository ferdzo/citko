import 'fake-indexeddb/auto';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import App from '../App';
import {api,login} from '../api';

vi.mock('../screens',()=>({Home:()=>null,Library:()=>null,BookDetails:()=>null,ReaderRoute:()=>null,Annotations:()=>null,Statistics:()=>null,Settings:()=>null,Imports:()=>null,Collections:()=>null,Upload:()=>null}));
vi.mock('../api',()=>({api:vi.fn(),login:vi.fn(),setup:vi.fn(),setCsrf:vi.fn()}));
beforeEach(()=>{localStorage.clear();vi.mocked(api).mockImplementation(async(path)=>{if(path==='/auth/me')throw new Error('Not signed in');if(path==='/auth/setup')return {required:false} as never;throw new Error(`Unexpected ${path}`)})});
afterEach(()=>{cleanup();vi.clearAllMocks()});
it('shows a useful error when the server rejects a login',async()=>{
 vi.mocked(login).mockRejectedValue(new Error('Email or password was not recognized.'));
 render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><App/></MemoryRouter></QueryClientProvider>);
 const person=userEvent.setup();await person.type(await screen.findByLabelText('Email address'),'reader@example.com');await person.type(screen.getByLabelText('Password'),'not-the-password');await person.click(screen.getByRole('button',{name:/sign in/i}));
 expect((await screen.findByRole('alert')).textContent).toContain('Email or password was not recognized.');
 expect(login).toHaveBeenCalledWith('reader@example.com','not-the-password');
});
it('asks for the setup token only when first-time setup is required',async()=>{
 vi.mocked(api).mockImplementation(async path=>{if(path==='/auth/me')throw new Error('Not signed in');if(path==='/auth/setup')return {required:true} as never;throw new Error('Unexpected API call')});
 render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><App/></MemoryRouter></QueryClientProvider>);
 await waitFor(()=>expect(screen.getByLabelText('Setup token')).not.toBeNull());
 expect(screen.getByLabelText('Your name')).not.toBeNull();
 expect(screen.getByRole('button',{name:/create reading room/i})).not.toBeNull();
});
