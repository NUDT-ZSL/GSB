"""GLSL 3.30 shaders for the offscreen ray-casting path."""

VERTEX_SHADER = r"""#version 330 core
const vec2 positions[3] = vec2[3](
    vec2(-1.0, -1.0),
    vec2( 3.0, -1.0),
    vec2(-1.0,  3.0)
);
void main() {
    gl_Position = vec4(positions[gl_VertexID], 0.0, 1.0);
}
"""


FRAGMENT_SHADER = r"""#version 330 core

out vec4 fragColor;

uniform usampler3D uVolume;
uniform sampler1D uTransferLut;

uniform vec3 uCameraPosition;
uniform vec3 uCameraForward;
uniform vec3 uCameraRight;
uniform vec3 uCameraTrueUp;
uniform vec2 uResolution;
uniform float uTanHalfFov;

uniform vec3 uBoundsMin;
uniform vec3 uBoundsMax;
uniform vec3 uOrigin;
uniform vec3 uSpacing;
uniform vec3 uVolumeShape;

uniform float uValueMin;
uniform float uValueMax;
uniform float uLutStep;
uniform int uLutSize;
uniform float uTransferMaxGradient;

uniform float uBaseStep;
uniform int uAdaptive;
uniform int uMaxRefinement;
uniform float uGradientThreshold;
uniform float uEarlyThreshold;
uniform int uMaxBlocks;
uniform int uMaxSubdivisions;

float sampleVolume(vec3 worldPosition) {
    vec3 q = (worldPosition - uOrigin) / uSpacing;
    q = clamp(q, vec3(0.0), uVolumeShape - vec3(1.0));
    ivec3 i0 = ivec3(floor(q));
    i0 = min(i0, ivec3(uVolumeShape) - ivec3(2));
    vec3 f = q - vec3(i0);
    int x0 = i0.x;
    int y0 = i0.y;
    int z0 = i0.z;
    int x1 = x0 + 1;
    int y1 = y0 + 1;
    int z1 = z0 + 1;

    float c000 = float(texelFetch(uVolume, ivec3(x0, y0, z0), 0).r);
    float c100 = float(texelFetch(uVolume, ivec3(x1, y0, z0), 0).r);
    float c010 = float(texelFetch(uVolume, ivec3(x0, y1, z0), 0).r);
    float c110 = float(texelFetch(uVolume, ivec3(x1, y1, z0), 0).r);
    float c001 = float(texelFetch(uVolume, ivec3(x0, y0, z1), 0).r);
    float c101 = float(texelFetch(uVolume, ivec3(x1, y0, z1), 0).r);
    float c011 = float(texelFetch(uVolume, ivec3(x0, y1, z1), 0).r);
    float c111 = float(texelFetch(uVolume, ivec3(x1, y1, z1), 0).r);

    float c00 = mix(c000, c100, f.x);
    float c01 = mix(c010, c110, f.x);
    float c10 = mix(c001, c101, f.x);
    float c11 = mix(c011, c111, f.x);
    float c0 = mix(c00, c01, f.y);
    float c1 = mix(c10, c11, f.y);
    return mix(c0, c1, f.z);
}

vec4 transferLookup(float scalarValue) {
    float normalized = clamp((scalarValue - uValueMin) / (uValueMax - uValueMin), 0.0, 1.0);
    int index = clamp(int(floor(normalized * float(uLutSize - 1) + 0.5)), 0, uLutSize - 1);
    return texelFetch(uTransferLut, index, 0);
}

float transferGradientNorm(float scalarValue) {
    float normalized = clamp((scalarValue - uValueMin) / (uValueMax - uValueMin), 0.0, 1.0);
    int index = clamp(int(floor(normalized * float(uLutSize - 1) + 0.5)), 0, uLutSize - 1);
    int below = max(index - 1, 0);
    int above = min(index + 1, uLutSize - 1);
    vec4 low = texelFetch(uTransferLut, below, 0);
    vec4 high = texelFetch(uTransferLut, above, 0);
    float denominator = max(float(above - below) * uLutStep, 1.0e-20);
    return length(high - low) / denominator;
}

bool intersectBox(vec3 rayOrigin, vec3 rayDirection, out float tNear, out float tFar) {
    vec3 low = uBoundsMin;
    vec3 high = uBoundsMax;
    float t0 = 0.0;
    float t1 = 1.0e30;
    for (int axis = 0; axis < 3; ++axis) {
        float origin = rayOrigin[axis];
        float direction = rayDirection[axis];
        float lower = low[axis];
        float upper = high[axis];
        if (abs(direction) < 1.0e-15) {
            if (origin < lower || origin > upper) {
                tNear = 0.0;
                tFar = 0.0;
                return false;
            }
        } else {
            float inverse = 1.0 / direction;
            float ta = (lower - origin) * inverse;
            float tb = (upper - origin) * inverse;
            float nearValue = min(ta, tb);
            float farValue = max(ta, tb);
            t0 = max(t0, nearValue);
            t1 = min(t1, farValue);
            if (t1 < t0) {
                tNear = 0.0;
                tFar = 0.0;
                return false;
            }
        }
    }
    tNear = t0;
    tFar = t1;
    return tFar > tNear;
}

int adaptiveLevel(float scalarValue) {
    if (uAdaptive == 0 || uMaxRefinement <= 0 || uTransferMaxGradient <= 0.0) {
        return 0;
    }
    float ratio = transferGradientNorm(scalarValue) / uTransferMaxGradient;
    if (ratio < uGradientThreshold) {
        return 0;
    }
    float requested = ceil(log2(max(ratio / uGradientThreshold, 1.0)));
    return int(clamp(max(requested, 1.0), 1.0, float(uMaxRefinement)));
}

void main() {
    vec2 ndc = gl_FragCoord.xy / uResolution;
    vec2 screen = ndc * 2.0 - 1.0;
    float aspect = uResolution.x / uResolution.y;
    vec3 direction = normalize(
        uCameraForward
        + screen.x * aspect * uTanHalfFov * uCameraRight
        + screen.y * uTanHalfFov * uCameraTrueUp
    );

    float tNear;
    float tFar;
    if (!intersectBox(uCameraPosition, direction, tNear, tFar)) {
        fragColor = vec4(0.0);
        return;
    }

    float rayLength = tFar - tNear;
    int blockCount = int(max(1.0, ceil(rayLength / uBaseStep - 1.0e-6)));
    float blockWidth = rayLength / float(blockCount);

    vec3 color = vec3(0.0);
    float transmittance = 1.0;
    bool terminated = false;

    for (int block = 0; block < uMaxBlocks; ++block) {
        if (block >= blockCount || terminated) {
            break;
        }
        float blockStart = tNear + float(block) * blockWidth;
        int subdivisions = 1;
        if (uAdaptive != 0) {
            vec3 probePosition = uCameraPosition + direction * (blockStart + 0.5 * blockWidth);
            float probeValue = sampleVolume(probePosition);
            subdivisions = 1 << adaptiveLevel(probeValue);
        }

        for (int subSample = 0; subSample < uMaxSubdivisions; ++subSample) {
            if (subSample >= subdivisions || terminated) {
                break;
            }
            float subWidth = blockWidth / float(subdivisions);
            float t = blockStart + (float(subSample) + 0.5) * subWidth;
            vec3 position = uCameraPosition + direction * t;
            float scalarValue = sampleVolume(position);
            vec4 mapped = transferLookup(scalarValue);
            float opacity = clamp(mapped.a, 0.0, 1.0);
            color += transmittance * opacity * mapped.rgb;
            transmittance *= (1.0 - opacity);
            if (uEarlyThreshold >= 0.0 && transmittance <= uEarlyThreshold) {
                transmittance = 0.0;
                terminated = true;
            }
        }
    }
    fragColor = vec4(color, clamp(1.0 - transmittance, 0.0, 1.0));
}
"""

