from __future__ import division
from .fragments import watershed_from_affinities
from funlib.segment.arrays import relabel, replace_values
from scipy.ndimage.measurements import center_of_mass
import daisy
import logging
import numpy as np

logger = logging.getLogger(__name__)

def upsample(a, factor):

    for d, f in enumerate(factor):
        a = np.repeat(a, f, axis=d)

    return a

def get_mask_data_in_roi(mask, roi, target_voxel_size):

    assert mask.voxel_size.is_multiple_of(target_voxel_size), (
        "Can not upsample from %s to %s" % (mask.voxel_size, target_voxel_size))

    aligned_roi = roi.snap_to_grid(mask.voxel_size, mode='grow')
    aligned_data = mask.to_ndarray(aligned_roi, fill_value=0)

    if mask.voxel_size == target_voxel_size:
        return aligned_data

    factor = mask.voxel_size/target_voxel_size

    upsampled_aligned_data = upsample(aligned_data, factor)

    upsampled_aligned_mask = daisy.Array(
        upsampled_aligned_data,
        roi=aligned_roi,
        voxel_size=target_voxel_size)

    return upsampled_aligned_mask.to_ndarray(roi)

def parallel_watershed(
        affs,
        rag_provider,
        block_size,
        context,
        fragments_out,
        num_workers,
        fragments_in_xy=False,
        epsilon_agglomerate=0,
        mask=None):
    '''Extract fragments from affinities using watershed.

    Args:

        affs (`class:daisy.Array`):

            An array containing affinities.

        rag_provider (`class:SharedRagProvider`):

            A RAG provider to write nodes for extracted fragments to. This does
            not yet add adjacency edges, for that, an agglomeration method
            should be called after this function.

        block_size (``tuple`` of ``int``):

            The size of the blocks to process in parallel in world units.

        context (``tuple`` of ``int``):

            The context to consider for fragment extraction, in world units.

        fragments_out (`class:daisy.Array`):

            An array to store fragments in. Should be of ``dtype`` ``uint64``.

        num_workers (``int``):

            The number of parallel workers.

        fragments_in_xy (``bool``):

            Whether to extract fragments for each xy-section separately.

        epsilon_agglomerate (``float``):

            Perform an initial waterz agglomeration on the extracted fragments
            to this threshold. Skip if 0 (default).

        mask (`class:daisy.Array`):

            A dataset containing a mask. If given, fragments are only extracted
            for masked-in (==1) areas.

    Returns:

        True, if all tasks succeeded.
    '''

    assert fragments_out.data.dtype == np.uint64

    if context is None:
        context = daisy.Coordinate((0,)*affs.roi.dims())
    else:
        context = daisy.Coordinate(context)

    total_roi = affs.roi.grow(context, context)
    read_roi = daisy.Roi((0,)*affs.roi.dims(), block_size).grow(context, context)
    write_roi = daisy.Roi((0,)*affs.roi.dims(), block_size)

    return daisy.run_blockwise(
        total_roi,
        read_roi,
        write_roi,
        lambda b: watershed_in_block(
            affs,
            b,
            rag_provider,
            fragments_out,
            fragments_in_xy,
            epsilon_agglomerate,
            mask),
        lambda b: block_done(b, rag_provider),
        num_workers=num_workers,
        read_write_conflict=False,
        fit='shrink')

def block_done(block, rag_provider):

    return rag_provider.num_nodes(block.write_roi) > 0


def to_pix(coords):
    return [coords[2]/4, coords[1]/4, coords[0]/40]


def watershed_in_block(
        affs,
        block,
        rag_provider,
        fragments_out,
        fragments_in_xy,
        epsilon_agglomerate,
        mask,
        myelin_ds=None):

    total_roi = affs.roi

    logger.debug("reading affs from %s", block.read_roi)
    affs = affs.intersect(block.read_roi)
    affs.materialize()

    if mask is not None:

        logger.debug("reading mask from %s", block.read_roi)
        mask_data = get_mask_data_in_roi(mask, affs.roi, affs.voxel_size)
        logger.debug("masking affinities")
        affs.data *= mask_data

    # extract fragments
    fragments_data, n = watershed_from_affinities(
        affs.data,
        fragments_in_xy=fragments_in_xy,
        epsilon_agglomerate=epsilon_agglomerate)
    if mask is not None:
        fragments_data *= mask_data.astype(np.uint64)
    fragments = daisy.Array(fragments_data, affs.roi, affs.voxel_size)

    # crop fragments to write_roi
    fragments = fragments[block.write_roi]
    fragments.materialize()

    # ensure we don't have IDs larger than the number of voxels (that would
    # break uniqueness of IDs below)
    max_id = fragments.data.max()
    if max_id > block.write_roi.size():
        logger.warning(
            "fragments in %s have max ID %d, relabelling...",
            block.write_roi, max_id)
        fragments.data, n = relabel(fragments.data)

    # ensure unique IDs
    size_of_voxel = daisy.Roi((0,)*affs.roi.dims(), affs.voxel_size).size()
    num_voxels_in_block = block.requested_write_roi.size()//size_of_voxel
    id_bump = block.block_id*num_voxels_in_block
    logger.info("bumping fragment IDs by %i", id_bump)
    # print(fragments.data[fragments.data>0])
    fragments.data[fragments.data>0] += id_bump
    fragment_ids = range(id_bump + 1, id_bump + 1 + n)

    # following only makes a difference if fragments were found
    if n == 0:
        return

    # get fragment centers
    fragment_centers = {
        fragment: block.write_roi.get_offset() + affs.voxel_size*center
        for fragment, center in zip(
            fragment_ids,
            center_of_mass(fragments.data, fragments.data, fragment_ids))
        if not np.isnan(center[0])
    }

    # post process myelin by setting these fragments to 0
    # and removing these from fragment_ids
    # TODO: setting affs to/from these fragments to 0??

    # This is the value that a fragment center is evaluated on
    MYELIN_PRED_LOW_THRESHOLD_CENTER = 48

    if myelin_ds is not None:
        myelin_fragments = []
        # myelin_ds.materialize()
        myelin_arr = myelin_ds[block.write_roi].to_ndarray()
        for node, c in fragment_centers.items():
            # quick check
            if myelin_ds[c] < MYELIN_PRED_LOW_THRESHOLD_CENTER:
                myelin_fragments.append(node)

            # slow check if center lies outside the fragment
            elif myelin_ds[c] < 225 or fragments[c] != node:
                # mean = np.ma.array(
                #     myelin_arr,
                #     mask=(fragments.data != node)).mean()
                # if mean < 48:
                #     myelin_fragments.append(node)

                arr = np.ma.array(
                    myelin_arr,
                    mask=(fragments.data != node)).compressed()
                quantile = np.quantile(arr, 0.15)
                if quantile < 150:
                    myelin_fragments.append(node)

            # if node in [161483646, 161483646]:
            #     # print("Node: %d" % node)
            #     # print("Center: %s" % c)
            #     # print("myelin_ds[c]: %s" % myelin_ds[c])
            #     # print("fragments[c]: %s" % fragments[c])
            #     arr = np.ma.array(
            #         myelin_arr,
            #         mask=(fragments.data != node)).compressed()
            #     quantile = np.quantile(arr, .15)
            #     print("Q at .15 for %d is %s" % (node, quantile))
            #     quantile = np.quantile(arr, .30)
            #     print("Q at .30 for %d is %s" % (node, quantile))
            #     quantile = np.quantile(arr, .40)
            #     print("Q at .40 for %d is %s" % (node, quantile))
            #     quantile = np.quantile(arr, .85)
            #     print("Q at .85 for %d is %s" % (node, quantile))
            #     print(arr)

                # else:
                #     print("Fragment %d at %s: mean %f" % (node, to_pix(c), mean))
                #     if fragments[c] == node:
                #         print("center val: %d" % myelin_ds[c])

            # else:
            #     print("Skipped fragment %d at %s: val %f" % (node, to_pix(c), myelin_ds[c]))

        for f in myelin_fragments:
            fragment_centers.pop(f)

        # relabel fragments
        replace_vals = [0 for i in myelin_fragments]
        replace_values(
            fragments.data,
            myelin_fragments,
            replace_vals,
            fragments.data)

    # store fragments
    logger.debug("writing fragments to %s", block.write_roi)
    fragments_out[block.write_roi] = fragments

    # store nodes
    rag = rag_provider[block.write_roi]
    rag.add_nodes_from([
        (node, {
            'center_z': c[0],
            'center_y': c[1],
            'center_x': c[2]
            }
        )
        for node, c in fragment_centers.items()
    ])
    rag.write_nodes(block.write_roi)
