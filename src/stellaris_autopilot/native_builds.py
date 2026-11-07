"""Reviewed executable profiles. The 4.5.3 profile permits observation/checkpoints only."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re


LEGACY_SHA256 = "400df27c82ddc845aa9dce79bd468d81f18f060d299cd263e3afa93aef4f7a83"
CYGNUS_453_SHA256 = "2f0fc2a91ccec9f77d390a5d198195927158c22444216a03596a23007fb3eafb"
SOURCES = ("native_research.js", "native_ship.js", "native_ship_colony.js",
           "native_planet.js", "native_fleet.js")

# Each entry was re-identified in the 4.5.3 executable; this is not a uniform shift.
_RVA_453 = {
    0x15bcf0: 0x15bc60, 0x15be90: 0x15be00, 0x160ff0: 0x160f20, 0x1ae1f0: 0x1ae120,
    0x3f3c50: 0x3f4680, 0x5f88a0: 0x5f9a10, 0x6b6620: 0x6b74d0, 0x6b9820: 0x6ba6d0,
    0x6baa80: 0x6bb930, 0x6bacf0: 0x6bbba0, 0x6baf60: 0x6bbe10, 0x6bb1d0: 0x6bc080,
    0x6bb4f0: 0x6bc3a0, 0x6d1b90: 0x6d2a80, 0x8cb860: 0x8ccf20, 0x961930: 0x963590,
    0xa590c0: 0xa5e010, 0xa5a430: 0xa5f380, 0xa5b9a0: 0xa608f0, 0xa70830: 0xa75840,
    0xa708a0: 0xa758b0, 0xa709b0: 0xa759c0, 0xacb850: 0xad08a0, 0xacc060: 0xad10b0,
    0xacd7d0: 0xad2820, 0xacd820: 0xad2870, 0xb0e680: 0xb13690, 0xccd000: 0xcd21a0,
    0xcd32b0: 0xcd8450, 0xe2f4c0: 0xe34e70, 0xeba020: 0xebf9f0, 0xebc230: 0xec1c00,
    0x23374d0: 0x233b4b0, 0x23914e8: 0x2395518, 0x2391f80: 0x2395cf0, 0x2392190: 0x2395fb0,
    0x23c1b60: 0x23c5a68, 0x23c1ef8: 0x23c5c90, 0x23c47e0: 0x23c88a0,
    0x31128e0: 0x3117910, 0x3112900: 0x3117930, 0x3112910: 0x3117938,
    0x31129d8: 0x3117a10, 0x3114670: 0x31196b8, 0x31146e0: 0x3119720,
    0x3114b90: 0x3119c18, 0x3114ba0: 0x3119c28, 0x3114c40: 0x3119cc8,
    0x3114c60: 0x3119ce8, 0x3114cd8: 0x3119d60, 0x3114ce0: 0x3119d68,
    0x3114d18: 0x3119da0, 0x3114e10: 0x3119e50, 0x3114e28: 0x3119e70,
    0x3114e68: 0x3119eb0, 0x3152ca8: 0x3157cc8,
}

# Before creating native functions or hooks, compare live code to these reviewed
# disk bytes. This also rejects a still-running old image after a Steam update.
_CORE_SIGNATURES_453 = (
    (0x15bc60, "48895c241048896c2418565741574883ec20488b6928498bf04c8bfa488bd94c"),
    (0x15be00, "40534883ec204883792810488bd9720e8339017409488b4910e8421fa101c643"),
    (0x160f20, "40534883ec204883793010488bd9720f837908017409488b4918e821cea001c6"),
    (0x1ae120, "b001c3cccccccccccccccccccccccccc4883ec28488bd1488d0df2d41802e8ad"),
    (0x3f4680, "48895c2408574881ec20050000410fb6d8488bf94533c9488d4c2420e89f6a7c"),
    (0x5f9a10, "48895c240848896c24104889742418574883ec300fb6ea488bf9488d058f2d21"),
    (0x6b74d0, "4889742410574883ec20488b158f29a602488bf14885d2742a448b4120418bc0"),
    (0x6ba6d0, "4883ec28b930000000e85abc960133d24889442430488d0d2caecd01c74008ff"),
    (0x6bb930, "4883ec38b920000000e8faa99601488b0d0bc2a50248c7442420fffffffff20f"),
    (0x6bbba0, "4883ec28b918000000e88aa79601488b0db3bea502488944243048894808488d"),
    (0x6bbe10, "4883ec38b920000000e81aa59601488b0d13bda50248c7442420fffffffff20f"),
    (0x6bc080, "4883ec38b920000000e8aaa29601488bd04889442440488b0523cfa502b9ffff"),
    (0x6bc3a0, "4883ec284c8b0515d9a5024c8bd24c8bc94d85c074288b51208bc225ffffff00"),
    (0x6d2a80, "4883ec28b930000000e8aa389501488bc8488944243033d2c74008ffffffff89"),
    (0x8ccf20, "40534881ec70050000b960050000e805947501488bd848898424800500000f57"),
    (0x963590, "4883ec28b958000000e89a2d6c014889442430c74008ffffffff33d289500cc7"),
    (0xa5e010, "4055565741544155415641574881ec90010000488d6c243048899da001000048"),
    (0xa5f380, "48895c2408488974241048897c24184c8974242055488d6c24d04881ec300100"),
    (0xa608f0, "48895c240848895424105556574154415541564157488d6c24d94881ec900000"),
    (0xa75840, "4883ec28b940000000e8ea0a5b0133d24889442430488d0dfc8e940148890848"),
    (0xa758b0, "4883ec28b988000000e87a0a5b01488bc84889442430c74008ffffffff4533c0"),
    (0xa759c0, "4883ec28b940000000e86a095b0133d24889442430488d0dac94940148890848"),
    (0xad08a0, "40554883ec60488d6c242048895d5048897d58488bda488b05cb7268024c6348"),
    (0xad10b0, "48895c24084889742410574883ec204c8b05fa8b6402488bf2488bf9b3014d85"),
    (0xad2820, "4883ec28b930000000e80a3b550133d24889442430488d0d2c328f01c74008ff"),
    (0xad2870, "4883ec28b928000000e8ba3a550133d24889442430488d0d04348f01c74008ff"),
    (0xb13690, "48895c242055488dac2470fbffff4881ec90050000b978050000e8892c510148"),
    (0xcd21a0, "4883ec28488bc148c1e820480fbec04883c0010f84a30000004883f801744b48"),
    (0xcd8450, "4c8b0d611244024c8bc1899130050000c78134050000ffffffff4d85c974258b"),
    (0xe34e70, "40534883ec20440fb64204488bd94183e8010f84450100004183e8010f84a200"),
    (0xebf9f0, "48894c2408555356574154415541564157488dac2478faffff4881ec88060000"),
    (0xec1c00, "48895c240848897c241055488d6c24a94881ecc0000000488bd933ff897db740"),
)


@dataclass(frozen=True)
class NativeBuild:
    sha256: str
    game_version: str
    observation_only: bool


_BUILDS = {
    LEGACY_SHA256: NativeBuild(LEGACY_SHA256, "Cygnus v4.5.2", False),
    CYGNUS_453_SHA256: NativeBuild(CYGNUS_453_SHA256, "Cygnus v4.5.3", True),
}


def build_for_hash(digest: str) -> NativeBuild | None:
    return _BUILDS.get(digest)


def build_for_executable(path: Path) -> NativeBuild:
    with Path(path).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    build = build_for_hash(digest)
    if build is None:
        raise ValueError("Unsupported game executable")
    return build


def supported_versions() -> list[str]:
    return [build.game_version for build in _BUILDS.values()]


# Preserve comments and string literals; only rewrite actual numeric tokens.
_JS_TOKEN = re.compile(r"/\*.*?\*/|//[^\n]*|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|0x[0-9a-fA-F]+",
                       re.DOTALL)


def render_native_source(build: NativeBuild, root: Path) -> str:
    if build_for_hash(build.sha256) != build:
        raise ValueError("Unreviewed native profile")
    source = "\n".join((Path(root) / name).read_text(encoding="utf-8") for name in SOURCES)
    if build.sha256 == LEGACY_SHA256:
        return source

    seen = set()

    def replace(match):
        text = match.group()
        if not text.startswith("0x"):
            return text
        address = int(text, 16)
        if address in _RVA_453:
            seen.add(address)
            return hex(_RVA_453[address])
        if address > 0x150000 and address != 0xffffff:
            raise ValueError("Unmapped native source address")
        return text

    source = _JS_TOKEN.sub(replace, source)
    if seen != set(_RVA_453):
        raise ValueError("Native source address inventory changed")
    signatures = json.dumps(_CORE_SIGNATURES_453)
    prefix = (
        "'use strict';\n"
        "/* Reviewed 4.5.3 translation; observation/checkpoint capability only. */\n"
        "{ const module = Process.getModuleByName('stellaris.exe');\n"
        f"for (const [rva, expected] of {signatures}) {{\n"
        "  const bytes = new Uint8Array(module.base.add(rva).readByteArray(expected.length / 2));\n"
        "  const actual = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');\n"
        "  if (actual !== expected) throw new Error('Loaded game code does not match the reviewed native profile');\n"
        "} }\n"
    )
    # This is a second gate inside the injected script. Even a direct worker RPC
    # cannot use the translated, statically located gameplay factories.
    capability_gate = (
        "\n{ const enqueue = rpc.exports.enqueue;\n"
        "rpc.exports.enqueue = function(request) {\n"
        "  const kind = request && request.kind;\n"
        "  if (!['request_observation','run_inbox','load_attached_events'].includes(kind) &&\n"
        "      !(kind === 'set_paused' && request.paused === true))\n"
        "    throw new Error('This build permits observation, pause and checkpoints only');\n"
        "  return enqueue(request);\n"
        "};\n"
        "rpc.exports.lookup = function() { throw new Error('Gameplay definitions use the versioned knowledge bundle'); };\n"
        "}\n"
    )
    return prefix + source + capability_gate
