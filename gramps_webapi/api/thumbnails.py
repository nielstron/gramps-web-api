"""Generate the same cached responses served by thumbnail endpoints."""

from flask import current_app
from .cache import thumbnail_cache, thumbnail_cache_key
from .media import get_media_handler

# Common frontend sizes, including high-DPI variants. Custom crops stay on demand.
THUMBNAIL_SIZES = (
    40,
    60,
    80,
    100,
    120,
    150,
    200,
    300,
    400,
    450,
    600,
    800,
    900,
    1200,
    2000,
)


def generate_media_thumbnails(db, tree, handle):
    media = db.get_media_from_handle(handle)
    if not (
        media.mime.startswith(("image/", "video/")) or media.mime == "application/pdf"
    ):
        return 0
    handler = get_media_handler(db, tree).get_file_handler(handle, db_handle=db)
    count = 0
    for size in THUMBNAIL_SIZES:
        path = f"/api/media/{handle}/thumbnail/{size}"
        for square in (False, True):
            query = {"square": str(square).lower()}
            key = thumbnail_cache_key(tree, media.checksum, path, query)
            # Repair overwrites cached responses as well as filling missing ones.
            with current_app.test_request_context(path, query_string=query):
                response = handler.send_thumbnail(size=size, square=square)
                response.direct_passthrough = False
                response.get_data()  # Materialize before closing the file wrapper.
                thumbnail_cache.set(key, response)
                response.close()
            count += 1
    return count


def schedule_media_thumbnails(handle):
    """Called after a committed upload; no request credentials enter job arguments."""
    from flask_jwt_extended import get_jwt_identity
    from .tasks import pregenerate_thumbnails, run_task
    from .util import get_tree_from_jwt_or_fail

    return run_task(
        pregenerate_thumbnails,
        tree=get_tree_from_jwt_or_fail(),
        user_id=get_jwt_identity(),
        handles=[handle],
    )
