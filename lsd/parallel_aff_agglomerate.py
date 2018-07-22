from .labels import relabel
import logging
import numpy as np
import peach
import waterz

logger = logging.getLogger(__name__)

def parallel_aff_agglomerate(
        affs,
        fragments,
        rag_provider,
        block_size,
        context,
        merge_function,
        threshold,
        num_workers):
    '''Extract fragments from affinities using watershed.

    Args:

        affs (array-like):

            A dataset that supports slicing to get affinities.

        fragments (array-like):

            A dataset that supports slicing to get fragments. Should be of
            ``dtype`` ``uint64``.

        rag_provider (`class:SharedRagProvider`):

            A RAG provider to write found edges to.

        block_size (``tuple`` of ``int``):

            The size of the blocks to process in parallel in voxels.

        context (``tuple`` of ``int``):

            The context to consider for agglomeration, in voxels.

        merge_function (``string``):

            The merge function to use for ``waterz``.

        threshold (``float``):

            Until which threshold to agglomerate.

        num_workers (``int``):

            The number of parallel workers.
    '''

    assert fragments.dtype == np.uint64

    shape = affs.shape[1:]
    context = peach.Coordinate(context)

    total_roi = peach.Roi((0,)*len(shape), shape).grow(context, context)
    read_roi = peach.Roi((0,)*len(shape), block_size).grow(context, context)
    write_roi = peach.Roi((0,)*len(shape), block_size)

    peach.run_with_dask(
        total_roi,
        read_roi,
        write_roi,
        lambda r, w: agglomerate_in_block(
            affs,
            fragments,
            rag_provider,
            r, w,
            merge_function,
            threshold),
        lambda w: block_done(w, rag_provider),
        num_workers=num_workers,
        read_write_conflict=False)

def block_done(write_roi, rag_provider):

    rag = rag_provider[write_roi.to_slices()]
    return rag.number_of_edges() > 0

def agglomerate_in_block(
        affs,
        fragments,
        rag_provider,
        read_roi,
        write_roi,
        merge_function,
        threshold):

    shape = fragments.shape
    affs_roi = peach.Roi((0,)*len(shape), shape)

    # ensure read_roi is within bounds of affs.shape
    read_roi = affs_roi.intersect(read_roi)

    logger.info(
        "Agglomerating in block %s with context of %s",
        write_roi, read_roi)

    # get the sub-{affs, fragments, graph} to work on
    affs = affs[(slice(None),) + read_roi.to_slices()]
    fragments = fragments[read_roi.to_slices()]
    rag = rag_provider[read_roi.to_slices()]

    # waterz uses memory proportional to the max label in fragments, therefore
    # we relabel them here and use those
    fragments_relabelled, n, fragment_relabel_map = relabel(
        fragments,
        return_backwards_map=True)

    logger.debug("affs shape: %s", affs.shape)
    logger.debug("fragments shape: %s", fragments.shape)
    logger.debug("fragments num: %d", n)

    # So far, 'rag' does not contain any edges. Run waterz until threshold 0 to
    # get the waterz RAG, which tells us which nodes are neighboring. Use this
    # to populate 'rag' with edges. Then run waterz for the given threshold.

    # for efficiency, we create one waterz call with both thresholds
    generator = waterz.agglomerate(
            affs=affs,
            thresholds=[0, threshold],
            fragments=fragments_relabelled,
            scoring_function=merge_function,
            discretize_queue=256,
            return_region_graph=True)

    # add edges to RAG
    _, initial_rag = generator.next()
    for edge in initial_rag:
        u, v = fragment_relabel_map[edge['u']], fragment_relabel_map[edge['v']]
        if rag.has_node(u) and rag.has_node(v):
            rag.add_edge(u, v, {'merged': False, 'agglomerated': True})

    # agglomerate fragments using affs
    segmentation, final_rag = generator.next()

    # map fragments to segments
    logger.debug("mapping fragments to segments...")
    stacked = np.stack([fragments.flatten(), segmentation.flatten()])
    fragment_segment_pairs = np.unique(stacked, axis=1).transpose()
    fragment_to_segment = {}
    for fragment, segment in fragment_segment_pairs:
        fragment_to_segment[fragment] = segment

    # mark edges in original RAG as 'merged'
    logger.debug("marking merged edges...")
    num_merged = 0
    for u, v, data in rag.edges(data=True):
        if fragment_to_segment[u] == fragment_to_segment[v]:
            data['merged'] = True
            num_merged += 1

    logger.info("merged %d edges", num_merged)

    # write back results (only within write_roi)
    logger.debug("writing to DB...")
    rag.sync_edges(write_roi)