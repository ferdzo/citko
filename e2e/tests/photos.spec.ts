import { expect, test, type Page } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

const email = process.env.E2E_EMAIL ?? 'reader@example.test';
const password = process.env.E2E_PASSWORD ?? 'citko-e2e-password-123';
const photo = path.join(__dirname, '../fixtures/citko-photo.jpg');

const FIXTURE_NAME = 'citko-photo.jpg';

interface PhotoRow {
  id: string;
  caption: string | null;
  fileName: string;
  favorite: boolean;
}

async function authenticate(page: Page) {
  await page.goto('/');
  const login = page.getByLabel('Email address');
  await expect(login).toBeVisible();
  await login.fill(email);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible();
}

async function photos(page: Page, baseURL: string, query = '') {
  const response = await page.request.get(`${baseURL}/api/v1/photos?page=1&pageSize=200${query}`);
  expect(response.ok()).toBeTruthy();
  return response.json() as Promise<{ items: PhotoRow[]; total: number }>;
}

async function csrfToken(page: Page, baseURL: string) {
  const response = await page.request.get(`${baseURL}/api/v1/auth/me`);
  expect(response.ok()).toBeTruthy();
  return ((await response.json()) as { csrfToken: string }).csrfToken;
}

/** The tiles are the only elements in the content area labelled "Open …"; the shell also has an
 *  "Open menu" button, which is why this is scoped to main. */
const tiles = (page: Page) => page.locator('main button[aria-label^="Open "]');

/** Cleanup is scoped to the fixture so a populated library is never wiped. */
test.afterEach(async ({ page, baseURL }) => {
  try {
    const token = await csrfToken(page, baseURL!);
    const { items } = await photos(page, baseURL!);
    for (const item of items.filter(row => row.fileName === FIXTURE_NAME)) {
      await page.request.delete(`${baseURL}/api/v1/photos/${item.id}`, { headers: { Origin: baseURL!, 'X-CSRF-Token': token } });
    }
  } catch {
    // Best effort: a failed run leaves at most one fixture photo behind on an isolated deployment.
  }
});

test('a photograph can be uploaded, inspected, captioned, favourited and deleted', async ({ page, baseURL }) => {
  await authenticate(page);
  const before = await photos(page, baseURL!);

  await page.getByRole('link', { name: 'Photos' }).click();
  await expect(page).toHaveURL(/\/photos$/);
  if (before.total === 0) {
    await expect(page.getByText('No photos yet')).toBeVisible();
  }

  // Upload through the real file chooser, exactly as a reader would.
  const [chooser] = await Promise.all([
    page.waitForEvent('filechooser'),
    page.getByRole('button', { name: /choose files/i }).click(),
  ]);
  await chooser.setFiles(photo);
  await page.getByRole('button', { name: /upload 1 photo/i }).click();

  await expect(tiles(page)).toHaveCount(1, { timeout: 30_000 });
  const thumbnail = tiles(page).first().locator('img');
  await expect.poll(async () => thumbnail.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBe(300);
  expect(await thumbnail.evaluate((element: HTMLImageElement) => element.naturalHeight)).toBe(480);

  // Hold the full-size response back so the stand-in is observable rather than raced against.
  await page.route('**/file', async route => {
    await new Promise(resolve => setTimeout(resolve, 3000));
    await route.continue();
  });

  await tiles(page).first().click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute('aria-label', 'Photo 1 of 1');

  const full = dialog.locator('img:not([aria-hidden])');
  const standIn = dialog.locator('img[aria-hidden="true"]');
  await expect.poll(async () => standIn.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBe(300);
  expect(await full.evaluate(element => getComputedStyle(element).opacity), 'the full-size photo is still hidden while in flight').toBe('0');

  await expect.poll(async () => full.evaluate((element: HTMLImageElement) => element.complete && element.naturalWidth > 0), { timeout: 20_000 }).toBe(true);
  await page.unroute('**/file');
  await expect.poll(async () => full.evaluate(element => getComputedStyle(element).opacity)).toBe('1');
  // Chrome applies the EXIF orientation: this fixture is 1600x1000 tagged to display turned.
  expect(await full.evaluate((element: HTMLImageElement) => [element.naturalWidth, element.naturalHeight])).toEqual([1000, 1600]);

  // The details panel reports what the camera recorded, as it is displayed.
  const details = await dialog.locator('dl').evaluate(list =>
    Object.fromEntries([...list.querySelectorAll(':scope > div')].map(row => [
      row.querySelector('dt')!.textContent!,
      row.querySelector('dd')!.textContent!,
    ])));
  expect(details).toMatchObject({
    Camera: 'Citko Cameras CK-1',
    Lens: 'CK 35mm f/2.8',
    ISO: 'ISO 400',
    Aperture: 'f/2.8',
    Shutter: '1/250',
    'Focal length': '35 mm',
    Dimensions: '1000 × 1600',
    Location: '51.5000, -0.1167',
    File: FIXTURE_NAME,
  });
  expect(JSON.stringify(details)).not.toMatch(/null|undefined|NaN/);

  // A caption survives a round trip to the server, not just the local view.
  await dialog.getByLabel('Caption').fill('Harbour at dawn');
  await dialog.getByLabel('Caption').press('Enter');
  await expect.poll(async () => (await photos(page, baseURL!)).items[0]?.caption).toBe('Harbour at dawn');

  await dialog.getByRole('button', { name: 'Add to favorites' }).click();
  await expect.poll(async () => (await photos(page, baseURL!, '&favorite=true')).total).toBe(1);

  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();

  // A file that is not an image is refused with something a reader can act on.
  const notAPhoto = test.info().outputPath('not-a-photo.txt');
  fs.writeFileSync(notAPhoto, 'this is not a picture\n');
  const [second] = await Promise.all([
    page.waitForEvent('filechooser'),
    page.getByRole('button', { name: /choose files/i }).click(),
  ]);
  await second.setFiles(notAPhoto);
  await page.getByRole('button', { name: /upload 1 photo/i }).click();
  await expect(page.getByText(/images are supported/i)).toBeVisible();

  // Deleting asks for confirmation, then the photo is gone from the server.
  await tiles(page).first().click();
  await expect(dialog).toBeVisible();
  page.once('dialog', confirmation => confirmation.accept());
  await dialog.getByRole('button', { name: 'Delete photo' }).click();
  await expect.poll(async () => (await photos(page, baseURL!)).total).toBe(before.total);
  await expect(page.getByText('No photos yet')).toBeVisible();

  // The uploaded photo is what was removed; unrelated photos were left alone.
  expect(await photos(page, baseURL!)).toMatchObject({ total: before.total });
});

test('the photo library and its lightbox fit a 390px phone', async ({ page, baseURL }) => {
  await authenticate(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/photos');
  await expect(page.getByRole('heading', { name: 'Photos' })).toBeVisible();

  const [chooser] = await Promise.all([
    page.waitForEvent('filechooser'),
    page.getByRole('button', { name: /choose files/i }).click(),
  ]);
  await chooser.setFiles(photo);
  await page.getByRole('button', { name: /upload 1 photo/i }).click();
  await expect(tiles(page)).toHaveCount(1, { timeout: 30_000 });

  const overflow = async () => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(await overflow()).toBeLessThanOrEqual(1);

  await tiles(page).first().click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel('Caption')).toBeVisible();
  expect(await overflow(), 'the open lightbox does not scroll the page sideways').toBeLessThanOrEqual(1);

  // On a phone the photograph stacks above the details rather than sitting beside them.
  const stacked = await dialog.evaluate(element => {
    const image = element.querySelector('img:not([aria-hidden])')!.getBoundingClientRect();
    const panel = element.querySelector('aside')!.getBoundingClientRect();
    return { imageBottom: Math.round(image.bottom), panelTop: Math.round(panel.top) };
  });
  expect(stacked.panelTop).toBeGreaterThanOrEqual(stacked.imageBottom - 1);

  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
});
