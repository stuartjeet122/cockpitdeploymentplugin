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

function scopedAsset(value) {
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
  return inPublic || generatedMedia || localApiRoute(segments) ? prefix + value : null;
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
  const pending = [{ node: tree, parent: null }];
  while (pending.length) {
    const { node, parent } = pending.pop();
    if (!node || typeof node !== 'object') continue;
    const moduleReference = parent && (
      ['ImportDeclaration', 'ExportNamedDeclaration', 'ExportAllDeclaration', 'ImportExpression'].includes(parent.type) ||
      (parent.type === 'CallExpression' &&
        (parent.callee.type === 'Import' || parent.callee.name === 'require'))
    );
    const value = node.type === 'StringLiteral' ? node.value :
      node.type === 'TemplateLiteral' && node.expressions.length === 0 ? node.quasis[0].value.cooked : null;
    const replacement = moduleReference ? null : scopedAsset(value);
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

walk(project);
process.stdout.write(`Prepared ${referencesChanged} local asset/API URL(s) in ${filesChanged} release file(s) for ${prefix}/\n`);