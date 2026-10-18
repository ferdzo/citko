import { expect, request as playwrightRequest, test, type APIRequestContext, type Page } from '@playwright/test';
import path from 'node:path';

const email = process.env.E2E_EMAIL ?? 'reader@example.test';
const password = process.env.E2E_PASSWORD ?? 'citko-e2e-password-123';
const name = process.env.E2E_NAME ?? 'Evidence Reader';
const setupToken = process.env.SETUP_TOKEN;
const epub = path.join(__dirname, '../fixtures/citko-evidence.epub');
const pdf = path.join(__dirname, '../fixtures/citko-evidence.pdf');

async function authenticate(page: Page) {
  await page.goto('/');
  const login = page.getByLabel('Email address');
  await expect(login).toBeVisible();
  await login.fill(email);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible();
}

async function readJson<T>(response: Awaited<ReturnType<APIRequestContext['get']>>): Promise<T> {
  return response.json() as Promise<T>;
}

test('setup/login, upload, real EPUB/PDF readers, persisted positions and explicit offline EPUB', async ({ browser, request, page, baseURL }) => {
  // Setup is intentionally one-time and only run when the deployment asks for it.
  const setupState = await request.get(`${baseURL}/api/v1/auth/setup`);
  expect(setupState.ok()).toBeTruthy();
  if ((await readJson<{ required: boolean }>(setupState)).required) {
    expect(setupToken, 'SETUP_TOKEN is required for a fresh deployment').toBeTruthy();
    const setup = await request.post(`${baseURL}/api/v1/auth/setup`, {
      headers: { 'X-Setup-Token': setupToken!, Origin: baseURL! },
      data: { email, password, name },
    });
    expect(setup.ok(), 'first administrator setup succeeds').toBeTruthy();
  }

  const contextA = page.context();
  const pageA = page;
  await authenticate(pageA);
  const login = await request.post(`${baseURL}/api/v1/auth/login`, { data: { email, password }, headers: { Origin: baseURL! } });
  expect(login.ok(), 'API login succeeds for this isolated deployment').toBeTruthy();

  // Upload through the actual UI and wait for worker state via visible ready state.
  await pageA.goto('/upload');
  await pageA.getByLabel('Choose book files').setInputFiles([epub, pdf]);
  await pageA.getByRole('button', { name: /upload 2 files/i }).click();
  await pageA.goto('/library');
  await expect(pageA.getByText('Citko Evidence: A Small Journey').first()).toBeVisible({ timeout: 120_000 });

  const list = await pageA.request.get(`${baseURL}/api/v1/books?page=1&pageSize=48`);
  expect(list.ok()).toBeTruthy();
  const books = (await readJson<{ items: Array<{ id: string; title: string; status: string; content: { format: string } | null }> }>(list)).items;
  const book = books.find(item => item.title === 'Citko Evidence: A Small Journey' && item.content?.format === 'epub');
  expect(book, 'uploaded EPUB is present in the API library').toBeTruthy();
  await expect.poll(async () => {
    const response = await pageA.request.get(`${baseURL}/api/v1/books/${book!.id}`);
    return (await response.json()).status;
  }, { timeout: 120_000 }).toBe('ready');

  // Exercise real sync receipt replay and stale-revision CAS behavior.
  const detailResponse = await pageA.request.get(`${baseURL}/api/v1/books/${book!.id}`);
  const detail = await detailResponse.json() as { revision: number; favorite: boolean };
  const csrfResponse = await pageA.request.get(`${baseURL}/api/v1/auth/csrf`);
  const csrf = (await csrfResponse.json() as { csrfToken: string }).csrfToken;
  const deviceId = crypto.randomUUID();
  const device = await pageA.request.post(`${baseURL}/api/v1/devices`, {
    headers: { 'X-CSRF-Token': csrf, Origin: baseURL! }, data: { id: deviceId, name: 'e2e replay device' },
  });
  expect(device.ok()).toBeTruthy();
  const replayedOp = { opId: crypto.randomUUID(), kind: 'book.patch', bookId: book!.id,
    baseRevision: detail.revision, value: { favorite: !detail.favorite } };
  const opsUrl = `${baseURL}/api/v1/sync/ops`;
  const firstReceipt = await pageA.request.post(opsUrl, {
    headers: { 'X-CSRF-Token': csrf, Origin: baseURL! }, data: { deviceId, ops: [replayedOp] },
  });
  expect(firstReceipt.ok()).toBeTruthy();
  const replayReceipt = await pageA.request.post(opsUrl, {
    headers: { 'X-CSRF-Token': csrf, Origin: baseURL! }, data: { deviceId, ops: [replayedOp] },
  });
  expect(replayReceipt.ok()).toBeTruthy();
  const firstResult = (await firstReceipt.json()).results[0];
  const replayResult = (await replayReceipt.json()).results[0];
  expect(firstResult).toMatchObject({ opId: replayedOp.opId, status: 'applied' });
  expect(replayResult).toEqual(firstResult);
  const staleOp = { opId: crypto.randomUUID(), kind: 'book.patch', bookId: book!.id,
    baseRevision: detail.revision, value: { favorite: detail.favorite } };
  const cas = await pageA.request.post(opsUrl, {
    headers: { 'X-CSRF-Token': csrf, Origin: baseURL! }, data: { deviceId, ops: [staleOp] },
  });
  expect(cas.ok()).toBeTruthy();
  expect((await cas.json()).results[0]).toMatchObject({ opId: staleOp.opId, status: 'conflict' });

  await pageA.goto(`/read/${book!.id}`);
  await expect(pageA.locator('foliate-view iframe').contentFrame().getByText('The river keeps the morning light', { exact: false })).toBeVisible({ timeout: 60_000 });

  // Capture real accepted position and verify it survives a reload and a distinct context.
  await pageA.getByRole('button', { name: 'Next page' }).last().click();
  await expect.poll(async () => {
    const r = await pageA.request.get(`${baseURL}/api/v1/books/${book!.id}`);
    return (await r.json()).progress;
  }, { timeout: 30_000 }).toBeGreaterThan(0);
  await expect.poll(async () => {
    const response = await pageA.request.get(`${baseURL}/api/v1/sync/changes?after=0&limit=500`);
    if (!response.ok()) return false;
    const body = await response.json();
    return body.changes?.some((change: { entity: string; value: { bookId?: string; locator?: { type?: string; cfi?: string } } }) =>
      change.entity === 'position' && change.value.bookId === book!.id && change.value.locator?.type === 'epub' && Boolean(change.value.locator.cfi));
  }, { timeout: 30_000 }).toBe(true);
  const contextB = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const pageB = await contextB.newPage();
  await authenticate(pageB);
  await pageB.goto(`/read/${book!.id}`);
  await expect(pageB.locator('foliate-view iframe').contentFrame().getByText('The river keeps the morning light', { exact: false })).toBeVisible({ timeout: 60_000 });
  await expect.poll(() => pageB.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await pageB.getByRole('button', { name: 'Table of contents' }).click();
  await expect(pageB.getByText('Your position here has not changed. Jump to another saved place only if you choose.')).toBeVisible({ timeout: 30_000 });
  await expect(pageB.getByRole('progressbar', { name: 'Book progress' })).toHaveAttribute('aria-valuenow', '0');
  await pageB.getByRole('button', { name: /\d+% ·/ }).click();
  await expect(pageB.getByRole('progressbar', { name: 'Book progress' })).not.toHaveAttribute('aria-valuenow', '0');
  await pageA.reload();
  await expect(pageA.getByText('Citko Evidence: A Small Journey')).toBeVisible({ timeout: 30_000 });
  await expect(pageA.getByRole('progressbar', { name: 'Book progress' })).not.toHaveAttribute('aria-valuenow', '0');

  // PDF engine evidence: open actual PDF book, require its selectable text layer and canvas.
  const pdfBook = books.find(item => item.content?.format === 'pdf');
  expect(pdfBook, 'the uploaded PDF is independently available in the library').toBeTruthy();
  await expect.poll(async () => {
    const response = await pageA.request.get(`${baseURL}/api/v1/books/${pdfBook!.id}`);
    return (await response.json()).status;
  }, { timeout: 120_000 }).toBe('ready');
  await pageA.goto(`/read/${pdfBook!.id}`);
  await expect(pageA.locator('.pdfViewer canvas').first()).toBeVisible({ timeout: 60_000 });
  const pdfTextLayer = pageA.locator('.textLayer').first();
  await expect(pdfTextLayer).toBeVisible({ timeout: 60_000 });
  await expect.poll(async () => pdfTextLayer.textContent()).toContain('The river keeps the morning light');
  await pageA.getByRole('button', { name: 'Search book' }).click();
  await pageA.getByRole('searchbox', { name: 'Search within book' }).fill('The Orchard');
  const pdfSearchResult = pageA.getByRole('button').filter({ hasText: 'The Orchard' }).last();
  await expect(pdfSearchResult).toBeVisible({ timeout: 30_000 });
  await pdfSearchResult.click();
  await expect.poll(async () => (await pageA.locator('.textLayer').allTextContents()).some(text => text.includes('The Orchard'))).toBe(true);
  await pageA.getByRole('button', { name: 'Next page' }).last().click();
  await expect(pageA.getByText('Page 2', { exact: true })).toBeVisible();
  await pageA.goto(`/books/${pdfBook!.id}`);
  await pageA.getByRole('link', { name: /Continue reading|Start reading/ }).click();
  await expect(pageA.locator('.pdfViewer canvas').first()).toBeVisible({ timeout: 60_000 });
  await expect(pageA.getByText('Page 2', { exact: true })).toBeVisible({ timeout: 30_000 });

  await pageA.goto(`/books/${book!.id}`);
  await pageA.getByRole('button', { name: 'Download for offline' }).click();
  await expect(pageA.getByText(/Available offline/)).toBeVisible({ timeout: 30_000 });
  await contextA.setOffline(true);
  await pageA.goto(`/read/${book!.id}`);
  await pageA.getByRole('button', { name: 'Table of contents' }).click();
  await pageA.getByRole('navigation', { name: 'Chapters' }).getByRole('button', { name: /The River/ }).click();
  const foliateText = pageA.locator('foliate-view iframe').contentFrame().getByText('The river keeps the morning light', { exact: false });
  await expect(foliateText).toBeVisible({ timeout: 60_000 });
  await pageA.getByRole('button', { name: 'Bookmark this position' }).click();
  await foliateText.evaluate(element => {
    const doc = element.ownerDocument;
    const range = doc.createRange();
    range.selectNodeContents(element);
    const selection = doc.getSelection();
    selection?.removeAllRanges(); selection?.addRange(range);
    doc.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
  });
  const annotate = pageA.getByRole('dialog', { name: 'Annotate selection' });
  await expect(annotate).toBeVisible();
  await annotate.getByRole('button', { name: 'Highlight' }).click();
  await expect(annotate).toBeHidden();
  await foliateText.evaluate(element => {
    const doc = element.ownerDocument;
    const range = doc.createRange(); range.selectNodeContents(element);
    const selection = doc.getSelection(); selection?.removeAllRanges(); selection?.addRange(range);
    doc.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
  });
  await expect(annotate).toBeVisible();
  await annotate.getByLabel('Add a thought').fill('Offline evidence note');
  await annotate.getByRole('button', { name: 'Save note' }).click();
  await expect(annotate).toBeHidden();
  await pageA.reload();
  await expect(pageA.locator('foliate-view iframe').contentFrame().getByText('The river keeps the morning light', { exact: false })).toBeVisible({ timeout: 60_000 });
  await pageA.getByRole('button', { name: 'Highlights and bookmarks' }).click();
  await expect(pageA.getByText('Offline evidence note')).toBeVisible();
  await expect(pageA.getByText('BOOKMARK')).toBeVisible();
  await contextA.setOffline(false);
  await expect.poll(async () => {
    const response = await pageA.request.get(`${baseURL}/api/v1/annotations?bookId=${book!.id}&page=1&pageSize=100`);
    if (!response.ok()) return [];
    return ((await response.json()).items as Array<{ note: string; type: string; id: string; quote: string }>)
      .filter(annotation => annotation.note === 'Offline evidence note' || annotation.type === 'bookmark' || (annotation.quote && annotation.type === 'highlight'));
  }, { timeout: 60_000 }).toMatchObject([
    expect.objectContaining({ note: 'Offline evidence note' }), expect.objectContaining({ type: 'bookmark' }),
  ]);
  const syncedAnnotations = await pageA.request.get(`${baseURL}/api/v1/annotations?bookId=${book!.id}&page=1&pageSize=100`);
  const matchingAnnotations = ((await syncedAnnotations.json()).items as Array<{ note: string; type: string; id: string; quote: string }>)
    .filter(annotation => annotation.note === 'Offline evidence note' || annotation.type === 'bookmark' || (annotation.quote && annotation.type === 'highlight'));
  expect(matchingAnnotations).toHaveLength(3);
  expect(matchingAnnotations.map(annotation => annotation.type).sort()).toEqual(['bookmark', 'highlight', 'note']);
  expect(new Set(matchingAnnotations.map(annotation => annotation.id)).size).toBe(matchingAnnotations.length);


  await contextA.close();
  await contextB.close();
});

test('denies another authenticated user access to an owned book file', async ({ request, baseURL }) => {
  test.skip(!process.env.E2E_OTHER_EMAIL || !process.env.E2E_OTHER_PASSWORD,
    'Set E2E_OTHER_EMAIL/PASSWORD to a separately provisioned account for ownership evidence.');
  const primaryLogin = await request.post(`${baseURL}/api/v1/auth/login`, { data: { email, password }, headers: { Origin: baseURL! } });
  expect(primaryLogin.ok()).toBeTruthy();
  const primaryBooks = await request.get(`${baseURL}/api/v1/books?page=1&pageSize=48`);
  const book = (await readJson<{ items: Array<{ id: string; content: { url: string } | null }> }>(primaryBooks)).items[0];
  expect(book).toBeTruthy();
  const attacker = await playwrightRequest.newContext({ baseURL });
  const credentials = await attacker.post(`${baseURL}/api/v1/auth/login`, {
    data: { email: process.env.E2E_OTHER_EMAIL, password: process.env.E2E_OTHER_PASSWORD }, headers: { Origin: baseURL! },
  });
  expect(credentials.ok()).toBeTruthy();
  const denied = await attacker.get(`/api/v1/books/${book.id}/file`);
  expect([403, 404]).toContain(denied.status());
  await attacker.dispose();
});
