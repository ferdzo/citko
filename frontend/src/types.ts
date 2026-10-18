export type UUID = string;
export type Format = 'epub' | 'pdf' | 'mobi' | 'fb2' | 'cbz';
export interface BookFile { id: UUID; format: Format | string; size: number; original: boolean }
export interface Rendition { renditionId: UUID; format: Format; sha256: string; size: number; url: string }
export interface Book {
  id: UUID; title: string; author: string; description: string; publisher: string; language: string;
  publishedAt: string | null; identifiers: Record<string,string>; tags: string[]; collectionIds: UUID[]; favorite: boolean;
  status: 'queued'|'processing'|'ready'|'failed'; error: string | null; readingStatus: 'unread'|'reading'|'completed'|'abandoned';
  completedAt: string | null; progress: number; createdAt: string; openedAt: string | null; revision: number;
  coverUrl: string | null; formats: string[]; content: Rendition | null; files: BookFile[];
}
export type Locator = {type:'epub';cfi:string} | {type:'pdf';page:number;y:number};
export type Anchor = {cfi:string;quote:string;prefix?:string;suffix?:string} | {rects:{page:number;x:number;y:number;width:number;height:number}[];quote:string;prefix?:string;suffix?:string};
export interface Annotation { id: UUID; bookId: UUID; renditionId: UUID; type:'highlight'|'note'|'bookmark'; locator: Locator; anchor: Anchor | null; quote:string; note:string; color:'yellow'|'green'|'blue'|'pink'; revision:number; deleted:boolean; createdAt:string }
export interface Position { id:string; bookId:UUID; deviceId:UUID; renditionId:UUID; locator:Locator; progress:number; revision:number; updatedAt:string }
export interface Collection {id:UUID;name:string;description:string;bookCount:number}
export interface ReaderPreferences {
  theme:'light'|'dark'|'sepia';fontFamily:string;fontSize:number;lineHeight:number;paragraphSpacing:number;margin:number;maxWidth:number;
  flow:'paginated'|'scrolled';pdfMode:'continuous'|'single';goalMinutes:number;goalBooks:number;
  bookOverrides:Record<string,Partial<Omit<ReaderPreferences,'bookOverrides'>>>;
}
export const defaultPreferences:ReaderPreferences = {theme:'light',fontFamily:'Georgia',fontSize:18,lineHeight:1.65,paragraphSpacing:1,margin:32,maxWidth:760,flow:'paginated',pdfMode:'continuous',goalMinutes:20,goalBooks:12,bookOverrides:{}};
export interface Page<T>{items:T[];total:number;page:number;pageSize:number}
export interface User{id:UUID;email:string;name:string}
export interface AuthResponse{user:User;csrfToken:string}
export interface Change {revision:number;entity:'book'|'position'|'annotation'|'preferences'|'stats'|'collection';id:string;value:unknown}
export type SyncOp =
  | {opId:string;kind:'position.put';bookId:string;renditionId:string;baseRevision:number|null;value:{locator:Locator;progress:number}}
  | {opId:string;kind:'annotation.put';annotationId:string;baseRevision:number|null;value:Omit<Annotation,'id'|'revision'|'deleted'|'createdAt'>}
  | {opId:string;kind:'annotation.delete';annotationId:string;baseRevision:number}
  | {opId:string;kind:'book.patch';bookId:string;baseRevision:number;value:Partial<Book>}
  | {opId:string;kind:'book.delete';bookId:string;baseRevision:number}
  | {opId:string;kind:'preferences.put';baseRevision:number|null;value:ReaderPreferences}
  | {opId:string;kind:'activity.append';eventId:string;bookId:string;sessionId:string;sequence:number;activeMs:number;localDate:string;timezoneOffset:number};
export interface SyncResult {opId:string;status:'applied'|'conflict'|'rejected';revision?:number;current?:unknown;code?:string;message?:string}
export interface Stats {totalActiveMs:number;booksStarted:number;booksCompleted:number;currentStreak:number;daily:{date:string;activeMs:number}[];byBook:{bookId:string;title:string;activeMs:number}[];recentSessions:{id:string;bookId:string;activeMs:number;receivedAt:string}[];goalMinutes:number;goalBooks:number}
export interface ImportJob{id:string;bookId:string|null;status:string;error:string|null;createdAt:string;[key:string]:unknown}
