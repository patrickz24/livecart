// Checks that every copy of a pinned tool version agrees with its source of
// truth, and that every GitHub Action is pinned by a full commit SHA.
//
//   pnpm run check:pins
//
// Sources of truth: .nvmrc (Node), packageManager in package.json (pnpm),
// .python-version (CPython) and [tool.uv] required-version in pyproject.toml
// (uv). Copies: package.json engines, the uv_build requirement of each uv
// member, the Dockerfiles and the CI workflow with its local actions. Every
// compose.yaml image carries an explicit version tag, and an image used in
// several files has the same tag everywhere.
//
// Files are read with line-oriented patterns instead of full parsers, so the
// script needs no dependency. A pattern that matches nothing is reported as a
// failure, never as a silent pass.
import { existsSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { isMainModule, requirePinnedNode } from "./cli.mjs";

export const PATHS = {
  nvmrc: ".nvmrc",
  pythonVersion: ".python-version",
  packageJson: "package.json",
  pyproject: "pyproject.toml",
  pythonDockerfile: "infra/docker/python.Dockerfile",
  webDockerfile: "infra/docker/web.Dockerfile",
  compose: "compose.yaml",
};

const EXACT_VERSION = /^v?\d+\.\d+\.\d+$/;
const COMMIT_SHA = /^[0-9a-f]{40}$/;
// An image tag that names a release: at least major.minor, never "latest".
const VERSIONED_TAG = /^v?\d+\.\d+/;

const lines = (text) => text.split(/\r?\n/);

/** Lower bound of a version specifier such as ">=0.12.19,<0.13". */
export function lowerBound(specifier) {
  return /(?:^|,)\s*>=\s*([0-9][^,\s]*)/.exec(specifier)?.[1];
}

/** Split an image reference into name and tag (a registry port is not a tag). */
export function splitImage(ref) {
  const [withoutDigest, digest] = ref.split("@");
  const colon = withoutDigest.lastIndexOf(":");
  if (colon > withoutDigest.lastIndexOf("/")) {
    return { name: withoutDigest.slice(0, colon), tag: withoutDigest.slice(colon + 1), digest };
  }
  return { name: withoutDigest, tag: undefined, digest };
}

/** Images of FROM and COPY --from= lines, stage names excluded. */
export function dockerfileImages(text) {
  const stages = new Set();
  const images = [];
  for (const [index, line] of lines(text).entries()) {
    const from = /^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?/i.exec(line);
    const copy = /^\s*COPY\b.*--from=(\S+)/i.exec(line);
    const ref = from?.[1] ?? copy?.[1];
    if (ref && !stages.has(ref.toLowerCase())) {
      images.push({ ref, line: index + 1, ...splitImage(ref) });
    }
    if (from?.[2]) stages.add(from[2].toLowerCase());
  }
  return images;
}

/** Values of `image:` keys; `${VAR:-default}` resolves to its default. */
export function yamlImages(text) {
  const images = [];
  for (const [index, line] of lines(text).entries()) {
    const match = /^\s*(?:-\s+)?image:\s*["']?([^"'#\s]+)["']?/.exec(line);
    if (!match) continue;
    const raw = match[1];
    const variable = /^\$\{[A-Z0-9_]+(?::?-([^}]+))?\}$/.exec(raw);
    const ref = variable ? variable[1] : raw;
    images.push({ raw, ref, line: index + 1, ...(ref ? splitImage(ref) : {}) });
  }
  return images;
}

/** `uses:` references with their trailing comment. */
export function actionUses(text) {
  const uses = [];
  for (const [index, line] of lines(text).entries()) {
    const match = /^\s*(?:-\s+)?uses:\s*["']?([^"'#\s]+)["']?\s*(?:#\s*(\S+))?/.exec(line);
    if (match) uses.push({ ref: match[1], comment: match[2], line: index + 1 });
  }
  return uses;
}

const isExpression = (value) => value.startsWith("${{");

/**
 * Literal values of a scalar key anywhere in a YAML file, with their line
 * numbers. GitHub expressions such as `${{ inputs.x }}` are not values: they
 * refer to a literal checked where it is defined.
 */
export function yamlValues(text, key) {
  const values = [];
  const pattern = new RegExp(`^\\s*(?:-\\s+)?${key}:\\s*["']?([^"'#\\s]+)["']?`);
  for (const [index, line] of lines(text).entries()) {
    const match = pattern.exec(line);
    if (match && !isExpression(match[1])) values.push({ value: match[1], line: index + 1 });
  }
  return values;
}

/** Workflow and local action files, in the order they were loaded. */
function ciFiles(files) {
  return Object.keys(files).filter(
    (file) =>
      /^\.github\/workflows\/[^/]+\.ya?ml$/.test(file) ||
      /^\.github\/actions\/[^/]+\/action\.ya?ml$/.test(file),
  );
}

function tomlString(text, key) {
  return new RegExp(`^\\s*${key}\\s*=\\s*"([^"]*)"`, "m").exec(text)?.[1];
}

function uvMembers(pyproject) {
  const list = /^\s*members\s*=\s*\[([^\]]*)\]/m.exec(pyproject)?.[1] ?? "";
  return [...list.matchAll(/"([^"]+)"/g)].map((match) => match[1]);
}

/**
 * Compare every pin. `files` maps repository-relative paths (forward slashes)
 * to their text; returns the checks that passed and the problems found.
 */
export function checkPins(files) {
  const passed = [];
  const problems = [];
  const read = (file) => {
    const text = files[file];
    if (text === undefined) problems.push(`${file}: file not found`);
    return text ?? "";
  };
  const expectSame = (label, expected, source, copies) => {
    const before = problems.length;
    if (copies.length === 0) problems.push(`${label}: no copy found to compare with ${source}`);
    for (const copy of copies) {
      if (copy.value !== expected) {
        problems.push(
          `${label}: ${copy.where} has ${copy.value ?? "nothing"}, expected ${expected} (${source})`,
        );
      }
    }
    if (problems.length === before) {
      passed.push(
        `${label} ${expected}: ${[source, ...copies.map((copy) => copy.where)].join(", ")}`,
      );
    }
  };

  const packageJson = JSON.parse(read(PATHS.packageJson) || "{}");
  const pythonDockerfile = dockerfileImages(read(PATHS.pythonDockerfile));
  const webDockerfileText = read(PATHS.webDockerfile);
  const webDockerfile = dockerfileImages(webDockerfileText);
  const ci = ciFiles(files);
  if (ci.length === 0) problems.push(".github: no workflow or local action found");

  // --- Node: .nvmrc ------------------------------------------------------------
  const node = read(PATHS.nvmrc).trim();
  const nodeCopies = [
    { where: "package.json engines.node", value: lowerBound(packageJson.engines?.node ?? "") },
    ...webDockerfile
      .filter((image) => image.name === "node")
      .map((image) => ({
        where: `${PATHS.webDockerfile}:${image.line}`,
        value: image.tag?.split("-")[0],
      })),
  ];
  for (const file of ci) {
    for (const { value, line } of yamlValues(files[file], "node-version-file")) {
      nodeCopies.push({
        where: `${file}:${line} node-version-file`,
        value: value === PATHS.nvmrc ? node : value,
      });
    }
    for (const { value, line } of yamlValues(files[file], "node-version")) {
      nodeCopies.push({ where: `${file}:${line} node-version`, value });
    }
  }
  expectSame("Node", node, PATHS.nvmrc, nodeCopies);

  // --- pnpm: packageManager ---------------------------------------------------
  const pnpm = /^pnpm@([^+]+)/.exec(packageJson.packageManager ?? "")?.[1] ?? "(missing)";
  const pnpmCopies = [
    { where: "package.json engines.pnpm", value: lowerBound(packageJson.engines?.pnpm ?? "") },
    ...[...webDockerfileText.matchAll(/\bpnpm@(\S+)/g)].map((match) => ({
      where: PATHS.webDockerfile,
      value: match[1],
    })),
  ];
  for (const file of ci) {
    for (const { value, line } of yamlValues(files[file], "PNPM_VERSION")) {
      pnpmCopies.push({ where: `${file}:${line} PNPM_VERSION`, value });
    }
  }
  expectSame("pnpm", pnpm, "package.json packageManager", pnpmCopies);

  // --- CPython: .python-version -------------------------------------------------
  const python = read(PATHS.pythonVersion).trim();
  expectSame(
    "CPython",
    python,
    PATHS.pythonVersion,
    pythonDockerfile
      .filter((image) => image.name === "python")
      .map((image) => ({
        where: `${PATHS.pythonDockerfile}:${image.line}`,
        value: image.tag?.split("-")[0],
      })),
  );

  // --- uv: required-version -----------------------------------------------------
  const pyproject = read(PATHS.pyproject);
  const uv = lowerBound(tomlString(pyproject, "required-version") ?? "") ?? "(missing)";
  const uvCopies = pythonDockerfile
    .filter((image) => image.name === "ghcr.io/astral-sh/uv")
    .map((image) => ({ where: `${PATHS.pythonDockerfile}:${image.line}`, value: image.tag }));
  for (const member of uvMembers(pyproject)) {
    const file = `${member}/pyproject.toml`;
    const requirement = /"uv_build([^"]*)"/.exec(read(file))?.[1] ?? "";
    uvCopies.push({ where: `${file} uv_build`, value: lowerBound(requirement) });
  }
  for (const file of ci) {
    for (const { value, line } of yamlValues(files[file], "UV_VERSION")) {
      uvCopies.push({ where: `${file}:${line} UV_VERSION`, value });
    }
  }
  expectSame("uv", uv, "pyproject.toml required-version", uvCopies);

  // --- Images: explicit version tags, one tag per image across files -----------
  const images = [
    ...yamlImages(read(PATHS.compose)).map((image) => ({ ...image, file: PATHS.compose })),
    ...pythonDockerfile.map((image) => ({ ...image, file: PATHS.pythonDockerfile })),
    ...webDockerfile.map((image) => ({ ...image, file: PATHS.webDockerfile })),
    ...ci.flatMap((file) => yamlImages(files[file]).map((image) => ({ ...image, file }))),
  ];
  const tagsByImage = new Map();
  const imageProblems = problems.length;
  for (const image of images) {
    const where = `${image.file}:${image.line}`;
    // Images built by compose itself (livecart-*:local) are not pulled.
    if (image.name?.startsWith("livecart-") && image.tag === "local") continue;
    if (!image.ref) {
      problems.push(`images: ${where} ${image.raw} has no default image`);
    } else if (!image.digest && !VERSIONED_TAG.test(image.tag ?? "")) {
      problems.push(`images: ${where} ${image.ref} needs an explicit version tag`);
    } else {
      const seen = tagsByImage.get(image.name);
      if (seen && seen.tag !== image.tag) {
        problems.push(
          `images: ${where} uses ${image.name}:${image.tag}, but ${seen.where} uses ${seen.tag}`,
        );
      } else if (!seen) {
        tagsByImage.set(image.name, { tag: image.tag, where });
      }
    }
  }
  if (problems.length === imageProblems) {
    passed.push(
      `images: ${[...tagsByImage].map(([name, { tag }]) => `${name}:${tag}`).join(", ")}`,
    );
  }

  // --- GitHub Actions: full commit SHA and a version comment -------------------
  const shaByAction = new Map();
  const actionProblems = problems.length;
  let pinnedActions = 0;
  for (const file of ci) {
    for (const use of actionUses(files[file])) {
      const where = `${file}:${use.line}`;
      if (use.ref.startsWith("./") || use.ref.startsWith("docker://")) continue;
      const [action, version] = use.ref.split("@");
      if (!version || !COMMIT_SHA.test(version)) {
        problems.push(`actions: ${where} ${use.ref} must be pinned by a full commit SHA`);
      } else if (!use.comment || !EXACT_VERSION.test(use.comment)) {
        problems.push(
          `actions: ${where} ${action} needs a "# vX.Y.Z" comment naming the pinned release`,
        );
      } else {
        const seen = shaByAction.get(action);
        if (seen && seen.sha !== version) {
          problems.push(`actions: ${where} pins ${action} to another commit than ${seen.where}`);
        }
        shaByAction.set(action, seen ?? { sha: version, where });
        pinnedActions += 1;
      }
    }
  }
  if (problems.length === actionProblems) {
    passed.push(
      `actions: ${pinnedActions} uses pinned by SHA (${[...shaByAction.keys()].join(", ")})`,
    );
  }

  // --- Other tool versions of the workflows: exact ------------------------------
  for (const file of ci) {
    for (const [index, line] of lines(files[file]).entries()) {
      const match = /^\s*([A-Z0-9_]+_VERSION):\s*["']?([^"'#\s]+)/.exec(line);
      if (match && !isExpression(match[2]) && !EXACT_VERSION.test(match[2])) {
        problems.push(
          `tools: ${file}:${index + 1} ${match[1]}=${match[2]} is not an exact version`,
        );
      }
    }
  }

  return { passed, problems };
}

/** Read every file the checks need from the repository at `root`. */
export function loadRepositoryFiles(root) {
  const files = {};
  const load = (file) => {
    const absolute = path.join(root, file);
    if (existsSync(absolute)) files[file] = readFileSync(absolute, "utf8");
  };
  Object.values(PATHS).forEach(load);
  uvMembers(files[PATHS.pyproject] ?? "").forEach((member) => load(`${member}/pyproject.toml`));

  const workflows = path.join(root, ".github", "workflows");
  if (existsSync(workflows)) {
    for (const name of readdirSync(workflows).sort()) {
      if (/\.ya?ml$/.test(name)) load(`.github/workflows/${name}`);
    }
  }
  const actions = path.join(root, ".github", "actions");
  if (existsSync(actions)) {
    for (const name of readdirSync(actions).sort()) {
      load(`.github/actions/${name}/action.yml`);
      load(`.github/actions/${name}/action.yaml`);
    }
  }
  return files;
}

// Run only as a command, not when the tests import the helpers above.
if (isMainModule(import.meta.url)) {
  requirePinnedNode();
  const root = fileURLToPath(new URL("..", import.meta.url));
  const { passed, problems } = checkPins(loadRepositoryFiles(root));
  for (const line of passed) console.log(`ok    ${line}`);
  for (const line of problems) console.error(`FAIL  ${line}`);
  if (problems.length > 0) {
    console.error(
      `\n${problems.length} pin problem(s). Update the copies to match their source of truth.`,
    );
    process.exit(1);
  }
  console.log("\nEvery pin agrees.");
}
