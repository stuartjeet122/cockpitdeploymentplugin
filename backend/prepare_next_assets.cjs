const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');

const project = fs.realpathSync(process.cwd());
const prefix = process.argv[2];
if (!/^\/[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*$/.test(prefix || '')) {
  throw new Error('Invalid application route for asset preparation');
}
const projectRequire = createRequire(path.join(project, 'package.json'));
const { parseSync } = projectRequire('next/dist/compiled/babel/core');
const ignored = new Set([
  'node_modules', '.git', '.next', 'out', 'dist', 'build', 'coverage',
  'public', 'tests', '__tests__', 'scripts', '.venv',
]);
const sourceExtensions = new Set(['.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.mts', '.cts']);
const mediaExtensions = new Set([
  '.svg', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif', '.ico',
  '.woff', '.woff2', '.ttf', '.otf', '.mp3', '.mp4', '.webm', '.ogg', '.wav',
]);

function regularAsset(root, segments) {
  let current = root;
  for (const segment of segments) {
    current = path.join(current, segment);
    if (!fs.existsSync(current) || fs.lstatSync(current).isSymbolicLink()) return false;
  }
  return fs.statSync(current).isFile();
}

function localApiRoute(segments) {
  if (segments[0] !== 'api') return false;
  for (const extension of sourceExtensions) {
    for (const directory of [['app'], ['src', 'app']]) {
      if (regularAsset(project, [...directory, ...segments, `route${extension}`])) return true;
    }
    for (const directory of [['pages'], ['src', 'pages']]) {
      if (regularAsset(project, [...directory, ...segments.slice(0, -1), segments.at(-1) + extension]) ||
          regularAsset(project, [...directory, ...segments, `index${extension}`])) return true;
    }
  }
  return false;
}

function localNextRoute(segments, includePages = false) {
  if (localApiRoute(segments)) return true;
  for (const directory of [['app'], ['src', 'app']]) {
    for (const extension of sourceExtensions) {
      if (includePages && regularAsset(project, [...directory, ...segments, `page${extension}`]) ||
          regularAsset(project, [...directory, ...segments, `route${extension}`])) return true;
      if (segments.at(-1) === 'manifest.webmanifest' &&
          regularAsset(project, [...directory, ...segments.slice(0, -1), `manifest${extension}`])) return true;
    }
    if (/^(?:icon|apple-icon|favicon|opengraph-image|twitter-image)(?:[.][a-z0-9]+)$/.test(segments.at(-1) || '') &&
        regularAsset(project, [...directory, ...segments])) return true;
  }
  return false;
}

function scopedAsset(value, manifestField = false) {
  if (manifestField && value === '/') return `${prefix}/`;
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') ||
      value === prefix || value.startsWith(`${prefix}/`) || value.includes('\\')) return null;
  const pathname = value.split(/[?#]/, 1)[0];
  let segments;
  try {
    segments = decodeURIComponent(pathname).slice(1).split('/');
  } catch {
    return null;
  }
  if (segments.some(segment => !segment || segment.startsWith('.') || segment.includes('\\'))) return null;
  const publicRoot = path.join(project, 'public');
  const inPublic = fs.existsSync(publicRoot) && !fs.lstatSync(publicRoot).isSymbolicLink() &&
    regularAsset(publicRoot, segments);
  const generatedMedia = segments.length === 1 && mediaExtensions.has(path.extname(pathname).toLowerCase()) &&
    regularAsset(project, segments);
  return inPublic || generatedMedia || localNextRoute(segments, manifestField) ? prefix + value : null;
}

let filesChanged = 0;
let referencesChanged = 0;
function prepareFile(filename) {
  const source = fs.readFileSync(filename, 'utf8');
  let tree;
  try {
    tree = parseSync(source, {
      filename,
      babelrc: false,
      configFile: false,
      sourceType: 'unambiguous',
      parserOpts: {
        plugins: [
          ...(path.extname(filename).includes('ts') ? ['typescript'] : []),
          ...(!path.extname(filename).includes('ts') || filename.endsWith('.tsx') ? ['jsx'] : []),
          'decorators-legacy',
        ],
      },
    });
  } catch {
    throw new Error(`Cannot safely prepare asset URLs in ${path.relative(project, filename)}`);
  }
  const edits = [];
  const pathnameHooks = new Set((tree.program?.body || []).filter(node =>
    node.type === 'ImportDeclaration' && node.source.value === 'next/navigation')
    .flatMap(node => node.specifiers.filter(specifier => specifier.imported?.name === 'usePathname')
      .map(specifier => specifier.local.name)));
  const pending = [{ node: tree, parent: null }];
  while (pending.length) {
    const { node, parent } = pending.pop();
    if (!node || typeof node !== 'object') continue;
    if (node.type === 'CallExpression' && pathnameHooks.has(node.callee.name) &&
        !(parent?.type === 'CallExpression' && parent.callee.type === 'ArrowFunctionExpression' &&
          parent.callee.params[0]?.name === 'appManagerPath')) {
      edits.push({ start: node.start, end: node.end, replacement:
        `(appManagerPath => appManagerPath && appManagerPath !== '/' ` +
        `? appManagerPath.replace(/\\/+$/, '') : appManagerPath)(${source.slice(node.start, node.end)})` });
      continue;
    }
    if (node.type === 'CallExpression' && node.callee.type === 'MemberExpression') {
      const method = node.callee.property.name;
      const receiver = node.callee.object;
      const historyCall = ['pushState', 'replaceState'].includes(method) &&
        (receiver.name === 'history' || receiver.type === 'MemberExpression' &&
          receiver.object.name === 'window' && receiver.property.name === 'history');
      const url = historyCall && node.arguments[2];
      if (url && !(url.type === 'CallExpression' && url.callee.type === 'ArrowFunctionExpression' &&
          url.callee.params[0]?.name === 'appManagerUrl')) {
        const literalPrefix = JSON.stringify(prefix);
        edits.push({ start: url.start, end: url.end, replacement:
          `(appManagerUrl => typeof appManagerUrl === 'string' && appManagerUrl.startsWith('/') && ` +
          `!appManagerUrl.startsWith('//') && appManagerUrl !== ${literalPrefix} && ` +
          `!appManagerUrl.startsWith(${literalPrefix} + '/') && !appManagerUrl.startsWith(${literalPrefix} + '?') && ` +
          `!appManagerUrl.startsWith(${literalPrefix} + '#') ? ${literalPrefix} + appManagerUrl : appManagerUrl)` +
          `(${source.slice(url.start, url.end)})` });
        continue;
      }
      if (method === 'requestFullscreen' && !(parent?.type === 'ConditionalExpression')) {
        edits.push({ start: node.start, end: node.end, replacement:
          `(typeof navigator !== 'undefined' && navigator.userActivation && !navigator.userActivation.isActive ` +
          `? Promise.resolve() : ${source.slice(node.start, node.end)})` });
        continue;
      }
    }
    const moduleReference = parent && (
      ['ImportDeclaration', 'ExportNamedDeclaration', 'ExportAllDeclaration', 'ImportExpression'].includes(parent.type) ||
      (parent.type === 'CallExpression' &&
        (parent.callee.type === 'Import' || parent.callee.name === 'require'))
    );
    const value = node.type === 'StringLiteral' ? node.value :
      node.type === 'TemplateLiteral' && node.expressions.length === 0 ? node.quasis[0].value.cooked : null;
    const manifestField = /^manifest\.[cm]?[jt]s$/.test(path.basename(filename)) && parent &&
      parent.type === 'ObjectProperty' && ['id', 'scope', 'start_url', 'src', 'url'].includes(parent.key.name || parent.key.value);
    const replacement = moduleReference ? null : scopedAsset(value, manifestField);
    if (replacement !== null) {
      const literal = parent && parent.type === 'JSXAttribute' ?
        source.slice(node.start, node.start + 1) + prefix + source.slice(node.start + 1, node.end) :
        JSON.stringify(replacement);
      edits.push({ start: node.start, end: node.end, replacement: literal });
      continue;
    }
    for (const child of Object.values(node)) {
      if (Array.isArray(child)) {
        for (const item of child) if (item && item.type) pending.push({ node: item, parent: node });
      } else if (child && child.type) {
        pending.push({ node: child, parent: node });
      }
    }
  }
  if (!edits.length) return;
  let prepared = source;
  for (const edit of edits.sort((first, second) => second.start - first.start)) {
    prepared = prepared.slice(0, edit.start) + edit.replacement + prepared.slice(edit.end);
  }
  fs.writeFileSync(filename, prepared);
  filesChanged += 1;
  referencesChanged += edits.length;
}

function walk(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    if (entry.name.startsWith('.') || entry.isSymbolicLink()) continue;
    const filename = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      if (!ignored.has(entry.name) && !fs.existsSync(path.join(filename, 'package.json'))) walk(filename);
    } else if (entry.isFile() && sourceExtensions.has(path.extname(entry.name)) &&
               !entry.name.startsWith('next.config.') && !/\.(?:test|spec|d)\.[cm]?[jt]sx?$/.test(entry.name)) {
      prepareFile(filename);
    }
  }
}

function prepareManifests(directory) {
  if (!fs.existsSync(directory) || fs.lstatSync(directory).isSymbolicLink()) return;
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    if (entry.isSymbolicLink() || entry.name.startsWith('.')) continue;
    const filename = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      prepareManifests(filename);
    } else if (entry.name === 'manifest.json' || entry.name.endsWith('.webmanifest')) {
      const source = fs.readFileSync(filename, 'utf8');
      let manifest;
      try { manifest = JSON.parse(source); } catch { throw new Error('Invalid generated web manifest'); }
      if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) throw new Error('Invalid generated web manifest');
      if (entry.name === 'manifest.json' && !('start_url' in manifest) && !('scope' in manifest)) continue;
      let count = 0;
      for (const key of ['id', 'scope', 'start_url']) {
        const value = manifest[key];
        if (typeof value === 'string' && value.startsWith('/') && !value.startsWith('//') &&
            value !== prefix && !value.startsWith(prefix + '/')) {
          manifest[key] = prefix + value;
          count += 1;
        }
      }
      for (const icon of Array.isArray(manifest.icons) ? manifest.icons : []) {
        if (icon && typeof icon.src === 'string') {
          const replacement = scopedAsset(icon.src, true);
          if (replacement !== null) { icon.src = replacement; count += 1; }
        }
      }
      if (count) {
        fs.writeFileSync(filename, JSON.stringify(manifest, null, 2) + '\n');
        filesChanged += 1;
        referencesChanged += count;
      }
    }
  }
}

if (process.argv[3] === '--install-hook') {
  const packagePath = path.join(project, 'package.json');
  const packageData = JSON.parse(fs.readFileSync(packagePath, 'utf8'));
  const wrapper = path.join(project, 'app-manager-build.cjs');
  if (!packageData.scripts?.build || fs.existsSync(wrapper) ||
      ['app-manager-original-build', 'preapp-manager-original-build', 'postapp-manager-original-build']
        .some(name => Object.hasOwn(packageData.scripts, name))) {
    throw new Error('Cannot install reserved App Manager build hook');
  }
  const manager = process.argv[4];
  const timeout = Number(process.argv[5]);
  if (!['npm', 'pnpm', 'yarn', 'bun'].includes(manager) || !Number.isSafeInteger(timeout) || timeout <= 0) {
    throw new Error('Invalid App Manager build hook arguments');
  }
  fs.writeFileSync(wrapper,
    `process.argv = [process.execPath, ${JSON.stringify(__filename)}, ${JSON.stringify(prefix)}, '--build', ` +
    `${JSON.stringify(manager)}, ${timeout}];\nrequire(${JSON.stringify(__filename)});\n`, { flag: 'wx' });
  packageData.scripts['app-manager-original-build'] = packageData.scripts.build;
  packageData.scripts.build = 'node app-manager-build.cjs';
  fs.writeFileSync(packagePath, JSON.stringify(packageData, null, 2) + '\n');
} else {
  walk(project);
  prepareManifests(path.join(project, 'public'));
  process.stdout.write(`Prepared ${referencesChanged} local asset/API URL(s) in ${filesChanged} release file(s) for ${prefix}/\n`);
  if (process.argv[3] === '--build') {
    const result = require('node:child_process').spawnSync(
      process.argv[4], ['run', 'app-manager-original-build'], {
        cwd: project, stdio: 'inherit', timeout: Number(process.argv[5]) * 1000,
        shell: process.platform === 'win32',
      });
    if (result.error) throw new Error('Original application build could not complete');
    process.exit(result.status ?? 1);
  }
}