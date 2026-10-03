"""Validate self-contained static SVG without rasterizing or stripping artwork.

Only a conservative subset of SVG/CSS is accepted. Reject unsupported markup
instead of changing its visual meaning. Uploaded SVGs are consumed as images,
but validation also keeps the stored bytes safe when opened independently.
"""

from io import StringIO
import re
import xml.etree.ElementTree as ET

from fastapi import HTTPException
import tinycss2

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"
XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
LOCAL_REFERENCE = re.compile(r"#[A-Za-z_][A-Za-z0-9_.:-]*\Z")

SVG_ELEMENTS = set(
    "svg g defs symbol use path rect circle ellipse line polyline polygon text tspan textPath "
    "linearGradient radialGradient stop clipPath mask pattern marker filter title desc style "
    "feBlend feColorMatrix feComponentTransfer feComposite feConvolveMatrix feDiffuseLighting "
    "feDisplacementMap feDistantLight feDropShadow feFlood feFuncA feFuncB feFuncG feFuncR "
    "feGaussianBlur feMerge feMergeNode feMorphology feOffset fePointLight feSpecularLighting "
    "feSpotLight feTile feTurbulence".split()
)
SVG_ATTRIBUTES = set(
    "id class width height x y x1 y1 x2 y2 cx cy r rx ry dx dy d points viewBox "
    "preserveAspectRatio transform pathLength href style version "
    "gradientUnits gradientTransform spreadMethod fx fy fr offset "
    "clipPathUnits maskUnits maskContentUnits patternUnits patternContentUnits patternTransform "
    "markerWidth markerHeight markerUnits refX refY orient "
    "filterUnits primitiveUnits in in2 result type values mode operator k1 k2 k3 k4 "
    "order kernelMatrix divisor bias targetX targetY edgeMode kernelUnitLength preserveAlpha "
    "surfaceScale diffuseConstant scale xChannelSelector yChannelSelector stdDeviation "
    "slope intercept amplitude exponent tableValues radius azimuth elevation limitingConeAngle "
    "specularConstant specularExponent pointsAtX pointsAtY pointsAtZ z seed baseFrequency "
    "numOctaves stitchTiles startOffset textLength lengthAdjust rotate method spacing "
    "role aria-label aria-hidden".split()
)
CSS_PROPERTIES = set(
    "alignment-baseline baseline-shift clip clip-path clip-rule color color-interpolation "
    "color-interpolation-filters color-rendering direction display dominant-baseline fill "
    "fill-opacity fill-rule filter flood-color flood-opacity font-family font-size "
    "font-size-adjust font-stretch font-style font-variant font-weight image-rendering "
    "isolation letter-spacing lighting-color marker marker-start marker-mid marker-end mask "
    "mix-blend-mode opacity overflow paint-order shape-rendering stop-color stop-opacity "
    "stroke stroke-dasharray stroke-dashoffset stroke-linecap stroke-linejoin stroke-miterlimit "
    "stroke-opacity stroke-width text-anchor text-decoration text-rendering transform "
    "transform-box transform-origin unicode-bidi vector-effect visibility white-space "
    "word-spacing writing-mode x y cx cy r rx ry width height".split()
)
CSS_FUNCTIONS = set(
    "rgb rgba hsl hsla hwb lab lch oklab oklch color color-mix calc min max clamp "
    "matrix matrix3d translate translatex translatey translatez translate3d scale scalex scaley "
    "scalez scale3d rotate rotatex rotatey rotatez rotate3d skew skewx skewy perspective".split()
)


def _invalid(reason: str):
    raise HTTPException(400, detail=f"Invalid or unsafe SVG: {reason}")


def _local_reference(value: str):
    if not LOCAL_REFERENCE.fullmatch(value.strip()):
        _invalid("references must be local # identifiers")


def _validate_css_tokens(tokens, *, css: bool, depth: int = 0):
    for token in tokens:
        if token.type in {"error", "at-keyword"}:
            _invalid("invalid CSS or unsupported CSS at-rule")
        if token.type == "url":
            _local_reference(token.value)
        elif token.type == "function":
            if depth >= 64:
                _invalid("CSS structure is too complex")
            if token.lower_name == "url":
                args = [t for t in token.arguments if t.type not in {"whitespace", "comment"}]
                if len(args) != 1 or args[0].type != "string":
                    _invalid("invalid CSS URL")
                _local_reference(args[0].value)
            else:
                if css and token.lower_name not in CSS_FUNCTIONS:
                    _invalid(f"unsupported CSS function: {token.name}")
                _validate_css_tokens(token.arguments, css=css, depth=depth + 1)
        elif hasattr(token, "content"):
            if depth >= 64:
                _invalid("CSS structure is too complex")
            _validate_css_tokens(token.content, css=css, depth=depth + 1)


def _validate_declarations(content):
    declarations = tinycss2.parse_declaration_list(
        content, skip_comments=True, skip_whitespace=True
    )
    for declaration in declarations:
        if declaration.type != "declaration" or declaration.lower_name not in CSS_PROPERTIES:
            _invalid("unsupported CSS declaration")
        _validate_css_tokens(declaration.value, css=True)


def _validate_stylesheet(content: str):
    rules = tinycss2.parse_stylesheet(content, skip_comments=True, skip_whitespace=True)
    for rule in rules:
        if rule.type != "qualified-rule":
            _invalid("unsupported CSS at-rule or invalid stylesheet")
        _validate_css_tokens(rule.prelude, css=False)
        _validate_declarations(rule.content)


def _expanded_name(name: str) -> tuple[str, str]:
    if name.startswith("{"):
        namespace, local = name[1:].split("}", 1)
        return namespace, local
    return "", name


def normalize_svg(contents: bytes) -> bytes:
    """Return normalized UTF-8 SVG or a clear validation error for unsafe input."""
    try:
        source = contents.decode("utf-8-sig")
    except UnicodeDecodeError:
        _invalid("use UTF-8 XML")
    # Reject declarations before XML parsing, so entities are never expanded and
    # no parser configuration can accidentally enable external resource loading.
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", source, re.IGNORECASE):
        _invalid("DTD and entities are not allowed")
    without_declaration = re.sub(r"^\s*<\?xml\s[^?]*\?>", "", source, count=1)
    if "<?" in without_declaration:
        _invalid("processing instructions are not allowed")
    try:
        namespaces = set()
        for _, declaration in ET.iterparse(StringIO(source), events=("start-ns",)):
            namespaces.add(declaration[1])
        if namespaces - {SVG_NAMESPACE, XLINK_NAMESPACE, XML_NAMESPACE}:
            _invalid("unsupported XML namespace")
        root = ET.fromstring(source)
        if root.tag != f"{{{SVG_NAMESPACE}}}svg":
            _invalid("root must be SVG with the SVG namespace")
        stack = [(root, 0)]
        count = 0
        while stack:
            node, depth = stack.pop()
            count += 1
            if depth > 64 or count > 10_000:
                _invalid("SVG structure is too complex")
            namespace, tag = _expanded_name(node.tag)
            if namespace != SVG_NAMESPACE or tag not in SVG_ELEMENTS:
                _invalid(f"unsupported element: {tag}")
            for name, value in node.attrib.items():
                namespace, attribute = _expanded_name(name)
                if namespace == XML_NAMESPACE:
                    if attribute not in {"lang", "space"}:
                        _invalid(f"unsupported XML attribute: {attribute}")
                    continue
                if namespace and not (namespace == XLINK_NAMESPACE and attribute == "href"):
                    _invalid("unsupported attribute namespace")
                if attribute.startswith("on") or attribute not in SVG_ATTRIBUTES | CSS_PROPERTIES:
                    _invalid(f"unsupported attribute: {attribute}")
                if attribute == "href":
                    _local_reference(value)
                elif attribute == "style":
                    _validate_declarations(value)
                elif attribute in CSS_PROPERTIES:
                    _validate_css_tokens(tinycss2.parse_component_value_list(value), css=True)
            if tag == "style":
                if len(node):
                    _invalid("style must contain CSS text only")
                _validate_stylesheet(node.text or "")
            stack.extend((child, depth + 1) for child in node)
        # The original namespace mappings need not be retained, but all expanded
        # names and artwork attributes are preserved. No active markup is stripped.
        return ET.tostring(root, encoding="utf-8")
    except (ET.ParseError, ValueError, RecursionError) as exc:
        raise HTTPException(400, detail="Invalid or excessively complex SVG XML/CSS") from exc
