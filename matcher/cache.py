"""Stage 5: content-addressed invalidation keys and revision tracking."""
from .errors import BusinessError, E_REVISION_REGRESS
from .models import content_hash, norm_float


def image_cache_key(image_id, cands, gts, threshold, classes):
    """Content key: GT revision bumps or threshold changes invalidate exactly
    the images whose content changed."""
    return content_hash(
        {
            "image_id": image_id,
            "candidates": [c.content() for c in sorted(cands, key=lambda c: c.box_idx)],
            "ground_truth": [g.content() for g in sorted(gts, key=lambda g: g.box_idx)],
            "iou_threshold": norm_float(threshold),
            "classes": sorted(classes),
        }
    )


def compute_keys(images, threshold, classes):
    return {
        image_id: image_cache_key(
            image_id,
            bundle["candidates"],
            bundle["ground_truth"],
            threshold,
            classes,
        )
        for image_id, bundle in images.items()
    }


def affected_images(old_keys, new_keys):
    """Images that are new, removed, or whose content key changed."""
    affected = set()
    for image_id, key in new_keys.items():
        if old_keys.get(image_id) != key:
            affected.add(image_id)
    for image_id in old_keys:
        if image_id not in new_keys:
            affected.add(image_id)
    return sorted(affected)


def check_revision_regress(old_gts, new_gts):
    """A ground-truth box arriving with a lower revision is rejected."""
    old_rev = {(g.image_id, g.box_idx): g.revision for g in old_gts}
    for g in new_gts:
        key = (g.image_id, g.box_idx)
        if key in old_rev and g.revision < old_rev[key]:
            raise BusinessError(
                E_REVISION_REGRESS,
                "ground-truth revision regressed for %s#%d: %d < %d"
                % (g.image_id, g.box_idx, g.revision, old_rev[key]),
                field="revision",
                context={
                    "image_id": g.image_id,
                    "box_idx": g.box_idx,
                    "revision": g.revision,
                    "committed_revision": old_rev[key],
                },
            )
