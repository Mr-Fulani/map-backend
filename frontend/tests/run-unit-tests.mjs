import { mkdirSync, mkdtempSync, rmSync, readdirSync } from 'node:fs';
import { dirname, join, resolve, sep } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const frontendDirectory = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const outputRoot = join(frontendDirectory, '.test-dist');
const expectedOutputRoot = resolve(frontendDirectory, '.test-dist');

if (resolve(outputRoot) !== expectedOutputRoot) {
  throw new Error('Refusing to use an unexpected unit-test output root');
}
mkdirSync(outputRoot, { recursive: true });
const outputDirectory = mkdtempSync(join(outputRoot, 'run-'));

if (!resolve(outputDirectory).startsWith(`${expectedOutputRoot}${sep}`)) {
  throw new Error('Refusing to clean an unexpected unit-test output directory');
}

function runNode(arguments_) {
  const result = spawnSync(process.execPath, arguments_, {
    cwd: frontendDirectory,
    stdio: 'inherit',
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    const error = new Error(`Unit-test subprocess exited with ${result.status}`);
    error.exitCode = result.status ?? 1;
    throw error;
  }
}

function findCompiledTests(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = join(directory, entry.name);
    if (entry.isDirectory()) return findCompiledTests(entryPath);
    return entry.isFile() && entry.name.endsWith('.test.js') ? [entryPath] : [];
  });
}

try {
  runNode([
    join(frontendDirectory, 'node_modules', 'typescript', 'bin', 'tsc'),
    '--project',
    join(frontendDirectory, 'tsconfig.test.json'),
    '--outDir',
    outputDirectory,
  ]);

  const compiledTestsDirectory = join(outputDirectory, 'tests');
  const testFiles = findCompiledTests(compiledTestsDirectory).sort();

  if (testFiles.length === 0) {
    throw new Error('Unit-test compilation produced no test files');
  }

  runNode([
    '--test',
    '--test-concurrency=1',
    '--test-timeout=10000',
    ...testFiles,
  ]);
} catch (error) {
  console.error(error);
  process.exitCode = Number.isInteger(error?.exitCode) ? error.exitCode : 1;
} finally {
  // Each process owns a unique child of .test-dist. Parallel test runs cannot
  // delete one another's compiled modules, and stale sources are never reused.
  rmSync(outputDirectory, { force: true, recursive: true });
}
