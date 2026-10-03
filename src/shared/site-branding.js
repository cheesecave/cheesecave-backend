// Shared by the public site and admin app. Keep this module dependency-free so
// branding can be restored locally before either application's API is ready.
import { getGifLoop } from "./gif-branding.js";

export { getGifLoop };
export const CACHE_KEY = "kohakuhub.site-branding.v1";
export const DEFAULT_BRANDING = Object.freeze({
  site_name: "KohakuHub",
  footer_description: "Self-hosted HuggingFace Hub alternative",
  header_logo: null,
  favicon: null,
});

const MAX_IMAGE_LENGTH = 350000;
const MAX_ASSET_BYTES = 256 * 1024;
const FETCH_TIMEOUT = 3000;
const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
const XLINK_NAMESPACE = "http://www.w3.org/1999/xlink";
const XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace";
const XMLNS_NAMESPACE = "http://www.w3.org/2000/xmlns/";
const LOCAL_REFERENCE = /^#[A-Za-z_][A-Za-z0-9_.:-]*$/;
// Keep the static SVG subset aligned with kohakuhub/svg_branding.py. Validate
// the browser cache too: its contents do not necessarily come from our API.
const svgElements = new Set(
  (
    "svg g defs symbol use path rect circle ellipse line polyline polygon text tspan textPath " +
    "linearGradient radialGradient stop clipPath mask pattern marker filter title desc style " +
    "feBlend feColorMatrix feComponentTransfer feComposite feConvolveMatrix feDiffuseLighting " +
    "feDisplacementMap feDistantLight feDropShadow feFlood feFuncA feFuncB feFuncG feFuncR " +
    "feGaussianBlur feMerge feMergeNode feMorphology feOffset fePointLight feSpecularLighting " +
    "feSpotLight feTile feTurbulence"
  ).split(" "),
);
const svgAttributes = new Set(
  (
    "id class width height x y x1 y1 x2 y2 cx cy r rx ry dx dy d points viewBox " +
    "preserveAspectRatio transform pathLength href style version " +
    "gradientUnits gradientTransform spreadMethod fx fy fr offset " +
    "clipPathUnits maskUnits maskContentUnits patternUnits patternContentUnits patternTransform " +
    "markerWidth markerHeight markerUnits refX refY orient " +
    "filterUnits primitiveUnits in in2 result type values mode operator k1 k2 k3 k4 " +
    "order kernelMatrix divisor bias targetX targetY edgeMode kernelUnitLength preserveAlpha " +
    "surfaceScale diffuseConstant scale xChannelSelector yChannelSelector stdDeviation " +
    "slope intercept amplitude exponent tableValues radius azimuth elevation limitingConeAngle " +
    "specularConstant specularExponent pointsAtX pointsAtY pointsAtZ z seed baseFrequency " +
    "numOctaves stitchTiles startOffset textLength lengthAdjust rotate method spacing " +
    "role aria-label aria-hidden"
  ).split(" "),
);
const cssProperties = new Set(
  (
    "alignment-baseline baseline-shift clip clip-path clip-rule color color-interpolation " +
    "color-interpolation-filters color-rendering direction display dominant-baseline fill " +
    "fill-opacity fill-rule filter flood-color flood-opacity font-family font-size " +
    "font-size-adjust font-stretch font-style font-variant font-weight image-rendering " +
    "isolation letter-spacing lighting-color marker marker-start marker-mid marker-end mask " +
    "mix-blend-mode opacity overflow paint-order shape-rendering stop-color stop-opacity " +
    "stroke stroke-dasharray stroke-dashoffset stroke-linecap stroke-linejoin stroke-miterlimit " +
    "stroke-opacity stroke-width text-anchor text-decoration text-rendering transform " +
    "transform-box transform-origin unicode-bidi vector-effect visibility white-space " +
    "word-spacing writing-mode x y cx cy r rx ry width height"
  ).split(" "),
);
const cssFunctions = new Set(
  (
    "rgb rgba hsl hsla hwb lab lch oklab oklch color color-mix calc min max clamp " +
    "matrix matrix3d translate translatex translatey translatez translate3d scale scalex scaley " +
    "scalez scale3d rotate rotatex rotatey rotatez rotate3d skew skewx skewy perspective"
  ).split(" "),
);

function cssText(value) {
  return value
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(
      /\\([0-9a-f]{1,6})(?:\r\n|[\t\n\f\r ])?|\\([^\n\r\f])/gi,
      (_, hex, character) => {
        const code = hex ? parseInt(hex, 16) : 0;
        return hex
          ? String.fromCodePoint(code > 0 && code <= 0x10ffff ? code : 0xfffd)
          : character;
      },
    );
}

function safeCssValue(value, decoded = false) {
  let valid = true;
  // Decode CSS escapes before checking resource references, including u\\72l().
  const text = (decoded ? value : cssText(value)).replace(
    /"[^"\n]*"|'[^'\n]*'|url\(\s*(?:"([^"\n]*)"|'([^'\n]*)'|([^()\s]*))\s*\)/gi,
    (match, doubleQuoted, singleQuoted, unquoted) => {
      // Quoted text outside url() is a literal, including font family names.
      if (match.startsWith('"') || match.startsWith("'")) return " ";
      if (
        !LOCAL_REFERENCE.test((doubleQuoted ?? singleQuoted ?? unquoted).trim())
      )
        valid = false;
      return " ";
    },
  );
  const tokens = text.replace(/"[^"\n]*"|'[^'\n]*'/g, " ");
  if (!valid || /[@\\{}]|url\s*\(/i.test(tokens)) return false;
  for (const match of tokens.matchAll(/([\w-]+)\s*\(/g)) {
    if (!cssFunctions.has(match[1].toLowerCase())) return false;
  }
  let depth = 0;
  for (const character of tokens) {
    if (character === "(" && ++depth > 64) return false;
    if (character === ")" && --depth < 0) return false;
  }
  return depth === 0;
}

function safeDeclarations(value, decoded = false) {
  const text = decoded ? value : cssText(value);
  // Strings may contain semicolons or colons, so retain their positions while
  // splitting declarations and validate each original value separately.
  const plain = text.replace(/"[^"\n]*"|'[^'\n]*'/g, (match) =>
    " ".repeat(match.length),
  );
  let offset = 0;
  for (const declaration of plain.split(";")) {
    if (declaration.trim()) {
      const colon = declaration.indexOf(":");
      if (
        colon < 0 ||
        !cssProperties.has(declaration.slice(0, colon).trim().toLowerCase()) ||
        !safeCssValue(
          text.slice(offset + colon + 1, offset + declaration.length),
          true,
        )
      )
        return false;
    }
    offset += declaration.length + 1;
  }
  return true;
}

function safeStylesheet(value) {
  const text = cssText(value);
  const plain = text.replace(/"[^"\n]*"|'[^'\n]*'/g, (match) =>
    " ".repeat(match.length),
  );
  let offset = 0;
  for (const match of plain.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (
      plain.slice(offset, match.index).trim() ||
      /[@\\]/.test(match[1]) ||
      !safeDeclarations(
        text.slice(
          match.index + match[1].length + 1,
          match.index + match[0].length - 1,
        ),
        true,
      )
    )
      return false;
    offset = match.index + match[0].length;
  }
  return !plain.slice(offset).trim();
}

function isSvg(encoded) {
  try {
    const decoded = atob(encoded);
    if (decoded.length > MAX_ASSET_BYTES) return false;
    const source = new TextDecoder("utf-8", { fatal: true }).decode(
      Uint8Array.from(decoded, (character) => character.charCodeAt(0)),
    );
    if (
      /<!\s*(?:DOCTYPE|ENTITY)\b/i.test(source) ||
      source.replace(/^\s*<\?xml\s[^?]*\?>/, "").includes("<?")
    )
      return false;
    const xml = new DOMParser().parseFromString(source, "image/svg+xml");
    const root = xml.documentElement;
    if (
      xml.querySelector("parsererror") ||
      root.localName !== "svg" ||
      root.namespaceURI !== SVG_NAMESPACE
    )
      return false;
    const stack = [[root, 0]];
    let count = 0;
    while (stack.length) {
      const [node, depth] = stack.pop();
      if (
        ++count > 10000 ||
        depth > 64 ||
        node.namespaceURI !== SVG_NAMESPACE ||
        !svgElements.has(node.localName)
      )
        return false;
      for (const attribute of node.attributes) {
        const { namespaceURI: namespace, localName: name, value } = attribute;
        if (namespace === XMLNS_NAMESPACE) {
          if (![SVG_NAMESPACE, XLINK_NAMESPACE, XML_NAMESPACE].includes(value))
            return false;
          continue;
        }
        if (namespace === XML_NAMESPACE) {
          if (!["lang", "space"].includes(name)) return false;
          continue;
        }
        if (namespace && !(namespace === XLINK_NAMESPACE && name === "href"))
          return false;
        if (!svgAttributes.has(name) && !cssProperties.has(name)) return false;
        if (name === "href" && !LOCAL_REFERENCE.test(value.trim()))
          return false;
        if (name === "style" && !safeDeclarations(value)) return false;
        if (cssProperties.has(name) && !safeCssValue(value)) return false;
      }
      if (
        node.localName === "style" &&
        (node.children.length || !safeStylesheet(node.textContent))
      )
        return false;
      for (const child of node.children) stack.push([child, depth + 1]);
    }
    return true;
  } catch {
    return false;
  }
}

function isImage(value) {
  if (value === null) return true;
  if (typeof value !== "string" || value.length > MAX_IMAGE_LENGTH)
    return false;
  const match =
    /^data:image\/(png|gif|svg\+xml);base64,([A-Za-z0-9+/]+={0,2})$/.exec(
      value,
    );
  if (!match || match[2].length % 4 !== 0) return false;
  if (match[1] === "gif") return getGifLoop(value) !== null;
  return match[1] === "png"
    ? match[2].startsWith("iVBORw0KGgo")
    : isSvg(match[2]);
}

export function normalizeBranding(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const { site_name, footer_description, header_logo, favicon } = value;
  if (
    typeof site_name !== "string" ||
    !site_name.trim() ||
    Array.from(site_name).length > 100 ||
    typeof footer_description !== "string" ||
    Array.from(footer_description).length > 2000 ||
    !isImage(header_logo) ||
    !isImage(favicon)
  ) {
    return null;
  }
  return {
    site_name: site_name.trim(),
    footer_description,
    header_logo,
    favicon,
  };
}

export function parseCachedBranding(raw) {
  try {
    const cache = JSON.parse(raw);
    return cache?.version === 1 ? normalizeBranding(cache.branding) : null;
  } catch {
    return null;
  }
}

export function readCachedBranding() {
  try {
    return (
      parseCachedBranding(globalThis.localStorage.getItem(CACHE_KEY)) || {
        ...DEFAULT_BRANDING,
      }
    );
  } catch {
    return { ...DEFAULT_BRANDING };
  }
}

export function saveCachedBranding(value) {
  const branding = normalizeBranding(value);
  if (!branding) return false;
  try {
    globalThis.localStorage.setItem(
      CACHE_KEY,
      JSON.stringify({ version: 1, branding }),
    );
    return true;
  } catch {
    // Storage can be unavailable or full. In-memory branding still works.
    return false;
  }
}

export function applyDocumentBranding(value, { admin = false } = {}) {
  const branding = normalizeBranding(value);
  if (!branding || typeof document === "undefined") return;
  document.title = `${branding.site_name}${admin ? " Admin Portal" : ""}`;
  const base = admin ? "/admin" : "";
  for (const [rel, fallback] of [
    ["icon", `${base}/favicon.svg`],
    ["apple-touch-icon", `${base}/images/logo-square.svg`],
  ]) {
    let link = document.head.querySelector(`link[rel="${rel}"]`);
    if (!link) {
      link = document.createElement("link");
      link.rel = rel;
      document.head.appendChild(link);
    }
    const href = branding.favicon || fallback;
    // Keep the existing icon when a refresh or text save leaves its URL unchanged.
    if (link.getAttribute("href") !== href) link.href = href;
    const type =
      branding.favicon?.match(/^data:(image\/[^;]+);/)?.[1] || "image/svg+xml";
    if (link.type !== type) link.type = type;
    if (rel === "icon" && link.hasAttribute("sizes"))
      link.removeAttribute("sizes");
  }
}

export async function fetchBranding() {
  const controller = new AbortController();
  let timer;
  try {
    return await Promise.race([
      (async () => {
        const response = await fetch("/api/site-branding", {
          signal: controller.signal,
          credentials: "same-origin",
          cache: "no-store",
        });
        if (
          !response.ok ||
          response.headers.get("X-Site-Branding-Fallback") === "true"
        ) {
          throw new Error("Site branding is unavailable");
        }
        const branding = normalizeBranding(await response.json());
        if (!branding) throw new Error("Invalid site branding response");
        return branding;
      })(),
      new Promise((_, reject) => {
        timer = setTimeout(() => {
          controller.abort();
          reject(new Error("Site branding request timed out"));
        }, FETCH_TIMEOUT);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}
