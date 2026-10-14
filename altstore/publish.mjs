#!/usr/bin/env node
// Uploads one file to Vercel Blob and prints its public URL.
//
// The IPA is larger than a Vercel static deployment may carry on the Hobby plan
// (100 MB), so it lives in Blob instead, where multipart uploads handle files
// far larger. A stable pathname (addRandomSuffix off, allowOverwrite on) keeps
// the download URL the same across republishes, which is what the source.json
// points at.
//
// It lives next to package.json (which pins @vercel/blob) so plain `import` resolves the package
// through the node_modules beside it; the Python generators stay in scripts/ with the rest.
//
// Usage: BLOB_READ_WRITE_TOKEN=... node altstore/publish.mjs <file> [pathname]
import { openAsBlob } from 'node:fs';
import { readFile, stat } from 'node:fs/promises';
import { basename } from 'node:path';
import { put } from '@vercel/blob';

const [file, pathnameArg] = process.argv.slice(2);
if (!file) {
  console.error('usage: publish.mjs <file> [pathname]');
  process.exit(1);
}

const pathname = pathnameArg || basename(file);
const { size } = await stat(file);

let body;
try {
  body = await openAsBlob(file, { type: 'application/octet-stream' });
} catch {
  // Node without fs.openAsBlob: fall back to loading it.
  body = await readFile(file);
}

const blob = await put(pathname, body, {
  access: 'public',
  addRandomSuffix: false,
  allowOverwrite: true,
  contentType: 'application/octet-stream',
  multipart: size > 100 * 1024 * 1024,
  token: process.env.BLOB_READ_WRITE_TOKEN,
});

process.stdout.write(blob.url + '\n');
