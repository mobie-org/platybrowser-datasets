from datetime import datetime

# TODO - uncomment this part
# this is a task called by multiple processes,
# so we need to restrict the number of threads used by numpy
# from cluster_tools.utils.numpy_utils import set_numpy_threads
# set_numpy_threads(1)
from tmp.numpy_utils import set_numpy_threads

set_numpy_threads(1)
import numpy as np

import h5py
import pandas as pd
from skimage.measure import regionprops, marching_cubes_lewiner, mesh_surface_area
from skimage.transform import resize
from skimage.util import pad
from scipy.ndimage.morphology import distance_transform_edt
from mahotas.features import haralick
from skimage.morphology import label, remove_small_objects


def log(msg):
    print("%s: %s" % (str(datetime.now()), msg))


# get shape of full data & downsampling factor
def get_scale_factor(path, key_full, key, resolution):
    with h5py.File(path, 'r') as f:
        full_shape = f[key_full].shape
        shape = f[key].shape

    # scale factor for downsampling
    scale_factor = [res * (fs / sh)
                    for res, fs, sh in zip(resolution,
                                           full_shape,
                                           shape)]
    return scale_factor


def filter_table(table, min_size, max_size):
    if max_size is None:
        table = table.loc[table['n_pixels'] >= min_size, :]
    else:
        criteria = np.logical_and(table['n_pixels'] > min_size, table['n_pixels'] < max_size)
        table = table.loc[criteria, :]
    return table


# some cell segmentations have a sensible total pixel size but very large bounding boxes i.e. they are small spots
# distributed over a large region, not one cell > filter for these cases by capping the bounding box size
def filter_table_bb(table, max_bb):
    total_bb = (table['bb_max_z'] - table['bb_min_z']) * (table['bb_max_y'] - table['bb_min_y']) * (
            table['bb_max_x'] - table['bb_min_x'])

    table = table.loc[total_bb < max_bb, :]

    return table


def filter_table_from_mapping(table, mapping_path):
    # read in numpy array of mapping of cells to nuclei - first column cell id, second nucleus id
    mapping = pd.read_csv(mapping_path, sep='\t')

    # remove zero labels from this table too, if exist
    mapping = mapping.loc[np.logical_and(mapping.iloc[:, 0] != 0,
                                         mapping.iloc[:, 1] != 0), :]
    table = table.loc[np.isin(table['label_id'], mapping['label_id']), :]

    # add a column for the 'nucleus_id' of the mapped nucleus (use this later to exclude the area covered
    # by the nucleus)
    table = table.join(mapping.set_index('label_id'), on='label_id', how='left')

    return table


# regions here are regions to exclude
def filter_table_region(table, region_path, regions=('empty', 'yolk', 'neuropil', 'cuticle')):
    region_mapping = pd.read_csv(region_path, sep='\t')

    # remove zero label if it exists
    region_mapping = region_mapping.loc[region_mapping['label_id'] != 0, :]

    for region in regions:
        region_mapping = region_mapping.loc[region_mapping[region] == 0, :]

    table = table.loc[np.isin(table['label_id'], region_mapping['label_id']), :]

    return table


def load_data(ds, row, scale):
    # compute the bounding box from the row information
    mins = [row.bb_min_z, row.bb_min_y, row.bb_min_x]
    maxs = [row.bb_max_z, row.bb_max_y, row.bb_max_x]
    mins = [int(mi / sca) for mi, sca in zip(mins, scale)]
    maxs = [int(ma / sca) + 1 for ma, sca in zip(maxs, scale)]
    bb = tuple(slice(mi, ma) for mi, ma in zip(mins, maxs))
    # load the data from the bounding box
    return ds[bb]


def morphology_row_features(mask, scale):
    # Calculate stats from skimage
    ski_morph = regionprops(mask.astype('uint8'))

    volume_in_pix = ski_morph[0]['area']
    volume_in_microns = np.prod(scale) * volume_in_pix
    extent = ski_morph[0]['extent']
    equiv_diameter = ski_morph[0]['equivalent_diameter']
    major_axis = ski_morph[0]['major_axis_length']
    minor_axis = ski_morph[0]['minor_axis_length']

    # The mesh calculation below fails if an edge of the segmentation is right up against the
    # edge of the volume - gives an open, rather than a closed surface
    # Pad by a few pixels to avoid this
    mask = pad(mask, 10, mode='constant')

    # surface area of mesh around object (other ways to calculate better?)
    verts, faces, normals, values = marching_cubes_lewiner(mask, spacing=tuple(scale))
    surface_area = mesh_surface_area(verts, faces)

    # sphericity (as in morpholibj)
    # Should run from zero to one
    sphericity = (36 * np.pi * (float(volume_in_microns) ** 2)) / (float(surface_area) ** 3)

    # max radius - max distance from pixel to outside
    edt = distance_transform_edt(mask, sampling=scale, return_distances=True)
    max_radius = np.max(edt)

    return (volume_in_microns, extent, equiv_diameter, major_axis,
            minor_axis, surface_area, sphericity, max_radius)


def intensity_row_features(raw, mask):
    intensity_vals_in_mask = raw[mask]
    # mean and stdev - use float64 to avoid silent overflow errors
    mean_intensity = np.mean(intensity_vals_in_mask, dtype=np.float64)
    st_dev = np.std(intensity_vals_in_mask, dtype=np.float64)
    median_intensity = np.median(intensity_vals_in_mask)

    quartile_75, quartile_25 = np.percentile(intensity_vals_in_mask, [75, 25])
    interquartile_range_intensity = quartile_75 - quartile_25

    total = np.sum(intensity_vals_in_mask, dtype=np.float64)

    return mean_intensity, st_dev, median_intensity, interquartile_range_intensity, total


def radial_intensity_row_features(raw, mask, scale, stops=(0.0, 0.25, 0.5, 0.75, 1.0)):
    result = ()

    edt = distance_transform_edt(mask, sampling=scale, return_distances=True)
    edt = edt / np.max(edt)

    bottoms = stops[0:len(stops) - 1]
    tops = stops[1:]

    radial_masks = [np.logical_and(edt > b, edt <= t) for b, t in zip(bottoms, tops)]

    for m in radial_masks:
        result += intensity_row_features(raw, m)

    return result


def texture_row_features(raw, mask):
    # errors if there are small, isolated spots (because I'm using ignore zeros as true)
    # so here remove components that are < 10 pixels
    # may still error in some cases
    labelled = label(mask)
    if len(np.unique(labelled)) > 2:
        labelled = remove_small_objects(labelled, min_size=10)
        mask = labelled != 0
        mask = mask.astype('uint8')

    # set regions outside mask to zero
    raw_copy = raw.copy()
    raw_copy[mask == 0] = 0

    try:
        hara = haralick(raw_copy, ignore_zeros=True, return_mean=True, distance=2)

    except ValueError:
        log('Texture computation failed - can happen when using ignore_zeros')
        hara = (0.,) * 13

    return tuple(hara)


def radial_distribution(edt, mask, stops=(0.0, 0.25, 0.5, 0.75, 1.0)):
    result = ()

    bottoms = stops[0:len(stops) - 1]
    tops = stops[1:]

    radial_masks = [np.logical_and(edt > b, edt <= t) for b, t in zip(bottoms, tops)]

    for m in radial_masks:
        result += (np.sum(mask[m]) / np.sum(m),)

    return result


def chromatin_row_features(chromatin, edt, raw, scale_chromatin):
    result = ()

    result += morphology_row_features(chromatin, scale_chromatin)

    # edt stats, dropping the total value
    result += intensity_row_features(edt, chromatin)[:-1]
    result += radial_distribution(edt, chromatin)

    if raw is not None:

        # resize the chromatin masks if not same size as raw
        if chromatin.shape != raw.shape:
            chromatin = resize(chromatin, raw.shape,
                               order=0, mode='reflect',
                               anti_aliasing=True, preserve_range=True).astype('bool')

        result += intensity_row_features(raw, chromatin)
        result += texture_row_features(raw, chromatin)

    return result


# compute morphology (and intensity features) for label range
def morphology_features_for_label_range(table, ds, ds_raw,
                                        ds_chromatin,
                                        ds_exclude,
                                        scale_factor_seg, scale_factor_raw,
                                        scale_factor_chromatin,
                                        scale_factor_exclude,
                                        label_begin, label_end):
    label_range = np.logical_and(table['label_id'] >= label_begin, table['label_id'] < label_end)
    sub_table = table.loc[label_range, :]
    stats = []
    for row in sub_table.itertuples(index=False):
        log(str(row.label_id))
        label_id = int(row.label_id)

        # load the segmentation data from the bounding box corresponding
        # to this row
        seg = load_data(ds, row, scale_factor_seg)

        # compute the segmentation mask and check that we have
        # foreground in the mask
        seg_mask = seg == label_id
        if seg_mask.sum() == 0:
            # if the seg mask is empty, we simply skip this label-id
            continue

        # compute the morphology features from the segmentation mask
        result = (float(label_id),) + morphology_row_features(seg_mask, scale_factor_seg)

        if ds_exclude is not None:
            exclude = load_data(ds_exclude, row, scale_factor_exclude)

            # resize to fit seg
            if exclude.shape != seg_mask.shape:
                exclude = resize(exclude, seg_mask.shape,
                                 order=0, mode='reflect',
                                 anti_aliasing=True, preserve_range=True).astype('bool')

            # binary for correct nucleus
            exclude = exclude == int(row.nucleus_id)

            # remove nucleus area form seg_mask
            seg_mask[exclude] = False

        # compute the intensity features from raw data and segmentation mask
        if ds_raw is not None:
            raw = load_data(ds_raw, row, scale_factor_raw)
            # resize the segmentation mask if it does not fit the raw data
            if seg_mask.shape != raw.shape:
                seg_mask = resize(seg_mask, raw.shape,
                                  order=0, mode='reflect',
                                  anti_aliasing=True, preserve_range=True).astype('bool')
            result += intensity_row_features(raw, seg_mask)
            result += radial_intensity_row_features(raw, seg_mask, scale_factor_raw)
            result += texture_row_features(raw, seg_mask)

        if ds_chromatin is not None:
            chromatin = load_data(ds_chromatin, row, scale_factor_chromatin)

            # set to 1 (heterochromatin), 2 (euchromatin)
            heterochromatin = chromatin == label_id + 12000
            euchromatin = chromatin == label_id

            # skip if no chromatin segmentation
            total_heterochromatin = heterochromatin.sum()
            total_euchromatin = euchromatin.sum()
            if total_heterochromatin == 0 and total_euchromatin.sum() == 0:
                continue

            # euclidean distance transform for whole nucleus, normalised to run from 0 to 1
            whole_nucleus = np.logical_or(heterochromatin, euchromatin)
            edt = distance_transform_edt(whole_nucleus, sampling=scale_factor_chromatin, return_distances=True)
            edt = edt / np.max(edt)

            if ds_raw is None:
                raw = None

            if total_heterochromatin != 0:
                result += chromatin_row_features(heterochromatin, edt, raw, scale_factor_chromatin)
            else:
                result += (0.,) * 36

            if total_euchromatin != 0:
                result += chromatin_row_features(euchromatin, edt, raw, scale_factor_chromatin)
            else:
                result += (0.,) * 36

        stats.append(result)
    return stats


def compute_morphology_features(table, segmentation_path, raw_path,
                                chromatin_path, exclude_nuc_path,
                                seg_key, raw_key, chromatin_key,
                                exclude_key,
                                scale_factor_seg, scale_factor_raw,
                                scale_factor_chromatin,
                                scale_factor_exclude,
                                label_starts, label_stops):
    if raw_path != '':
        assert raw_key is not None and scale_factor_raw is not None
        f_raw = h5py.File(raw_path, 'r')
        ds_raw = f_raw[raw_key]
    else:
        f_raw = ds_raw = None

    if chromatin_path != '':
        assert chromatin_key is not None and scale_factor_chromatin is not None
        f_chromatin = h5py.File(chromatin_path, 'r')
        ds_chromatin = f_chromatin[chromatin_key]
    else:
        f_chromatin = ds_chromatin = None

    if exclude_nuc_path != '':
        assert exclude_key is not None and scale_factor_exclude is not None
        f_exclude = h5py.File(exclude_nuc_path, 'r')
        ds_exclude = f_exclude[exclude_key]

    else:
        f_exclude = ds_exclude = None

    with h5py.File(segmentation_path, 'r') as f:
        ds = f[seg_key]

        stats = []
        for label_a, label_b in zip(label_starts, label_stops):
            log("Computing features from label-id %i to %i" % (label_a, label_b))
            stats.extend(morphology_features_for_label_range(table, ds, ds_raw,
                                                             ds_chromatin,
                                                             ds_exclude,
                                                             scale_factor_seg, scale_factor_raw,
                                                             scale_factor_chromatin,
                                                             scale_factor_exclude,
                                                             label_a, label_b))
    if f_raw is not None:
        f_raw.close()

    if f_chromatin is not None:
        f_chromatin.close()

    if f_exclude is not None:
        f_exclude.close()

    # convert to pandas table and add column names
    stats = pd.DataFrame(stats)
    columns = ['label_id']
    morph_columns = ['shape_volume_in_microns', 'shape_extent', 'shape_equiv_diameter',
                     'shape_major_axis', 'shape_minor_axis', 'shape_surface_area', 'shape_sphericity',
                     'shape_max_radius']

    columns += morph_columns

    if raw_path != '':
        intensity_columns = ['intensity_mean', 'intensity_st_dev', 'intensity_median', 'intensity_iqr',
                             'intensity_total']
        texture_columns = ['texture_hara%s' % x for x in range(1, 14)]

        columns += intensity_columns
        # radial intensity columns
        for val in [25, 50, 75, 100]:
            columns += ['%s_%s' % (var, val) for var in intensity_columns]
        columns += texture_columns

    if chromatin_path != '':
        edt_columns = ['shape_edt_mean', 'shape_edt_stdev', 'shape_edt_median', 'shape_edt_iqr']
        edt_columns += ['shape_percent_%s' % var for var in [25, 50, 75, 100]]

        for phase in ['_het', '_eu']:
            columns += [var + phase for var in morph_columns]
            columns += [var + phase for var in edt_columns]

            if raw_path != '':
                columns += [var + phase for var in intensity_columns]
                columns += [var + phase for var in texture_columns]

    stats.columns = columns
    return stats


def morphology_impl(segmentation_path, raw_path, chromatin_path,
                    exclude_nuc_path,
                    table, mapping_path,
                    region_mapping_path,
                    min_size, max_size,
                    max_bb,
                    resolution, raw_scale, seg_scale,
                    chromatin_scale, exclude_nuc_scale,
                    label_starts, label_stops):
    """ Compute morphology features for a segmentation.

    Can compute features for multiple label ranges. If you want to
    compute features for the full label range, pass
    'label_starts=[0]' and 'label_stops=[number_of_labels]'

    Arguments:
        segmentation_path [str] - path to segmentation stored as h5.
        raw_path [str] - path to raw data stored as h5.
            Pass 'None' if you don't want to compute features based on raw data.
        chromatin_path [str] - path to chromatin segmentation data stored as h5.
            Pass 'None' if you don't want to compute features based on chromatin.
        exclude_nuc_path [str] - path to nucleus segmentation data stored as h5.
            Pass 'None' if you don't want to use the nucleus segmentation as a mask to exclude this region.
        table [pd.DataFrame] - table with default attributes
            (sizes, center of mass and bounding boxes) for segmentation
        mapping_path [str] - path to - path to nucleus id mapping.
            Pass 'None' if not relevant.
        region_mapping_path [str] - path to - path to cellid to region mapping
            Pass 'None' if not relevant
        min_size [int] - minimal size for objects used in calculation
        max_size [int] - maximum size for objects used in calculation
        max_bb [int] - maximum total volume of bounding box for objects used in calculation
        resolution [listlike] - resolution in nanometer.
            Must be given in [Z, Y, X].
        raw_scale [int] - scale level of the raw data
        seg_scale [int] - scale level of the segmentation
        chromatin_scale [int] - scale level of the chromatin segmentation
        exclude_nuc_scale [int] - scale level of the nucleus segmentation
        label_starts [listlike] - list with label start positions
        label_stops [listlike] - list with label stop positions
    """

    # keys to segmentation and raw data for the different scales
    seg_key_full = 't00000/s00/0/cells'
    seg_key = 't00000/s00/%i/cells' % seg_scale
    raw_key_full = 't00000/s00/0/cells'
    raw_key = 't00000/s00/%i/cells' % raw_scale
    chromatin_key_full = 't00000/s00/0/cells'
    chromatin_key = 't00000/s00/%i/cells' % chromatin_scale
    exclude_key_full = 't00000/s00/0/cells'
    exclude_key = 't00000/s00/%i/cells' % exclude_nuc_scale

    # get scale factor for the segmentation
    scale_factor_seg = get_scale_factor(segmentation_path, seg_key_full, seg_key, resolution)

    # get scale factor for raw data (if it's given)
    if raw_path != '':
        log("Have raw path; compute intensity features")
        # NOTE for now we can hard-code the resolution for the raw data here,
        # but we might need to change this if we get additional dataset(s)
        raw_resolution = [0.025, 0.01, 0.01]
        scale_factor_raw = get_scale_factor(raw_path, raw_key_full, raw_key, raw_resolution)
    else:
        log("Don't have raw path; do not compute intensity features")
        raw_resolution = scale_factor_raw = None

    # get scale factor for chromatin (if it's given)
    if chromatin_path != '':
        log("Have chromatin path; compute chromatin features")
        # NOTE for now we can hard-code the resolution for the chromatin data here,
        # but we might need to change this if we get additional dataset(s)
        chromatin_resolution = [0.025, 0.02, 0.02]
        scale_factor_chromatin = get_scale_factor(chromatin_path, chromatin_key_full, chromatin_key,
                                                  chromatin_resolution)
    else:
        log("Don't have chromatin path; do not compute chromatin features")
        chromatin_resolution = scale_factor_chromatin = None

    # get scale factor for nuclei segmentation (to be used to exclude nucleus) if given
    if exclude_nuc_path != '':
        log("Have nucleus path; exclude nucleus for intensity measures")
        # NOTE for now we can hard-code the resolution for the nuclei data here,
        # but we might need to change this if we get additional dataset(s)
        exclude_resolution = [0.1, 0.08, 0.08]
        scale_factor_exclude = get_scale_factor(exclude_nuc_path, exclude_key_full, exclude_key,
                                                exclude_resolution)
    else:
        log("Don't have exclude path; don't exclude nucleus area for intensity measures")
        scale_factor_exclude = scale_factor_exclude = None

    # remove zero label if it exists
    table = table.loc[table['label_id'] != 0, :]

    # if we have a mapping, only keep objects in the mapping
    # (i.e cells that have assigned nuclei)
    if mapping_path != '':
        log("Have mapping path %s" % mapping_path)
        table = filter_table_from_mapping(table, mapping_path)
        log("Number of labels after filter with mapping: %i" % table.shape[0])

    if region_mapping_path != '':
        log("Have region mapping path %s" % region_mapping_path)
        table = filter_table_region(table, region_mapping_path)
        log("Number of labels after region filter: %i" % table.shape[0])

    # filter by size
    table = filter_table(table, min_size, max_size)
    log("Number of labels after size filter: %i" % table.shape[0])

    # filter by bounding box size
    if max_bb is not None:
        table = filter_table_bb(table, max_bb)
        log("Number of labels after bounding box size filter %i" % table.shape[0])

    log("Computing morphology features")
    stats = compute_morphology_features(table, segmentation_path, raw_path,
                                        chromatin_path, exclude_nuc_path,
                                        seg_key, raw_key, chromatin_key,
                                        exclude_key,
                                        scale_factor_seg, scale_factor_raw,
                                        scale_factor_chromatin,
                                        scale_factor_exclude,
                                        label_starts, label_stops)
    return stats


if __name__ == '__main__':
    pass
