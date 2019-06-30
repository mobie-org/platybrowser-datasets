#! /g/kreshuk/pape/Work/software/conda/miniconda3/envs/cluster_env37/bin/python

import os
import argparse
from subprocess import check_output, call
from shutil import rmtree

from scripts.attributes import make_cell_tables, make_nucleus_tables
from scripts.export import export_segmentation
from scripts.files import copy_image_data, copy_misc_data
from scripts.files import copy_tables, copy_segmentation, make_folder_structure


# paths for paintera projects
# in order to get the new segmentation, changes need to be committed,
# s.t. they are stored in these files!
PAINTERA_ROOT = '/g/kreshuk/data/arendt/platyneris_v1/data.n5'
PROJECT_CELLS = 'volumes/paintera/proofread_cells'
PROJECT_NUCLEI = 'volumes/paintera/nuclei'

# name for cell and nucleus segmentations
NAME_CELLS = 'sbem-6dpf-1-whole-segmented-cells-labels'
NAME_NUCLEI = 'sbem-6dpf-1-whole-segmented-nuclei-labels'

# resolutions of cell and nucleus segmentation
RES_CELLS = [.025, .02, .02]
RES_NUCLEI = [.1, .08, .08]


def check_inputs(update_cell_segmentation,
                 update_nucleus_segmentation,
                 update_cell_tables,
                 update_nucleus_tables):
    inputs = (update_cell_segmentation, update_nucleus_segmentation,
              update_cell_tables, update_nucleus_tables)
    have_changes = any(inputs)
    if update_cell_segmentation:
        update_cell_tables = True
    if update_nucleus_segmentation:
        update_nucleus_tables = True
    return have_changes, update_cell_tables, update_nucleus_tables


def get_tags():
    tag = check_output(['git', 'tag']).decode('utf-8').rstrip('\n')
    new_tag = tag.split('.')
    new_tag[-1] = str(int(new_tag[-1]) + 1)
    new_tag = '.'.join(new_tag)
    return tag, new_tag


def export_segmentations(folder, new_folder,
                         update_cell_segmentation,
                         update_nucleus_segmentation,
                         target, max_jobs):
    # update or copy cell segmentation
    if update_cell_segmentation:
        tmp_cells_seg = 'tmp_export_cells'
        export_segmentation(PAINTERA_ROOT, PROJECT_CELLS,
                            folder, new_folder, NAME_CELLS,
                            resolution=RES_CELLS,
                            tmp_folder=tmp_cells_seg,
                            target=target, max_jobs=max_jobs)
    else:
        copy_segmentation(folder, new_folder, NAME_CELLS)

    # update or copy nucleus segmentation
    if update_nucleus_segmentation:
        tmp_nuc_seg = 'tmp_export_nuclei'
        export_segmentation(PAINTERA_ROOT, PROJECT_NUCLEI,
                            folder, new_folder, NAME_NUCLEI,
                            resolution=RES_NUCLEI,
                            tmp_folder=tmp_nuc_seg,
                            target=target, max_jobs=max_jobs)
    else:
        copy_segmentation(folder, new_folder, NAME_NUCLEI)

    # copy static segmentations
    static_seg_names = ('sbem-6dpf-1-whole-segmented-muscles',
                        'sbem-6dpf-1-whole-segmented-tissue-labels')
    for seg_name in static_seg_names:
        copy_segmentation(folder, new_folder, seg_name)


def make_attributes(folder, new_folder,
                    update_cell_tables, update_nucleus_tables,
                    target, max_jobs):
    # update or copy cell tables
    if update_cell_tables:
        make_cell_tables(new_folder, NAME_CELLS, 'tmp_tables_cells', RES_CELLS,
                         target=target, max_jobs=max_jobs)
    else:
        copy_tables(folder, new_folder, NAME_CELLS)

    # update or copy nucleus tables
    if update_nucleus_tables:
        make_nucleus_tables(new_folder, NAME_NUCLEI, 'tmp_tables_nuclei', RES_NUCLEI,
                         target=target, max_jobs=max_jobs)
    else:
        copy_tables(folder, new_folder, NAME_NUCLEI)

    # TODO
    # copy tables associated with static segmentations
    static_seg_names = ('sbem-6dpf-1-whole-segmented-tissue-labels',)
    for seg_name in static_seg_names:
        copy_tables(folder, new_folder, seg_name)


# TODO check for errors
def make_release(tag, folder, description=''):
    call(['git', 'add', folder])
    call(['git', 'commit', '-m', 'Automatic platybrowser update'])
    if description == '':
        call(['git', 'tag', tag])
    else:
        call(['git', '-m', description, 'tag', tag])
    # TODO autopush ???
    # call(['git', 'push', 'origin', 'master', '--tags'])


def clean_up():

    def remove_dir(dir_name):
        try:
            rmtree(dir_name)
        except OSError:
            pass

    # remove all tmp folders
    remove_dir('tmp_export_cells')
    remove_dir('tmp_export_nuclei')
    remove_dir('tmp_tables_cells')
    remove_dir('tmp_tables_nuclei')


# TODO catch all exceptions and handle them properly
def update_platy_browser(update_cell_segmentation=False,
                         update_nucleus_segmentation=False,
                         update_cell_tables=False,
                         update_nucleus_tables=False,
                         description=''):
    """ Generate new version of platy-browser derived data.

    Arguments:
        update_cell_segmentation: Update the cell segmentation volume.
        update_nucleus_segmentation: Update the nucleus segmentation volume.
        update_cell_tables: Update the cell tables. This needs to be specified if the cell
            segmentation is not update, but the tables should be updated.
        update_nucleus_tables: Update the nucleus tables. This needs to be specified if the nucleus
            segmentation is not updated, but the tables should be updated.
        description: Optional descrption for release message.
    """

    # check inputs
    have_changes, update_cell_tables, update_nucleus_tables = check_inputs(update_cell_segmentation,
                                                                           update_nucleus_segmentation,
                                                                           update_cell_tables,
                                                                           update_nucleus_tables)
    if not have_changes:
        print("Nothing needs to be update, skipping")
        return

    # we always increase the release tag (in the last digit)
    # when making a new version of segmentation or attributes
    tag, new_tag = get_tags()
    print("Updating platy browser from", tag, "to", new_tag)

    # make new folder structure
    folder = os.path.join('data', tag)
    new_folder = os.path.join('data', new_tag)
    make_folder_structure(new_folder)

    target = 'slurm'
    max_jobs = 250

    # copy static image and misc data
    copy_image_data(os.path.join(folder, 'images'),
                    os.path.join(new_folder, 'images'))
    copy_misc_data(os.path.join(folder, 'misc'),
                   os.path.join(new_folder, 'misc'))

    # export new segmentations
    export_segmentations(folder, new_folder,
                         update_cell_segmentation,
                         update_nucleus_segmentation,
                         target=target, max_jobs=max_jobs)

    # generate new attribute tables
    make_attributes(folder, new_folder,
                    update_cell_tables,
                    update_nucleus_tables,
                    target=target, max_jobs=max_jobs)

    # make new release
    make_release(new_tag, new_folder, description)
    print("Updated platybrowser to new release", new_tag)
    print("All changes were successfully made. Starting clean up now.")
    print("This can take a few hours, you can already use the new data; clean up will only remove temp files.")

    # TODO would be better to start this as a job on a different machine, not the login node!
    # TODO clean up tmp folders
    # clean_up()


def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def table_help_str(name):
    help_str = """Update the %s tables.
                  Only needs to be specified if the %s segmentation is not updated,
                  but the tables should be updated."""
    return help_str % (name, name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Update derived data for the platy browser')

    parser.add_argument('--update_cell_segmentation', type=str2bool,
                        default=False, help="Update the cell segmentation.")
    parser.add_argument('--update_nucleus_segmentation', type=str2bool,
                        default=False, help="Update the nucleus segmentation.")
    parser.add_argument('--update_cell_tables', type=str2bool,
                        default=False, help=table_help_str("cell"))
    parser.add_argument('--update_nucleus_tables', type=str2bool,
                        default=False, help=table_help_str("nucleus"))
    parser.add_argument('--description', type=str, default='',
                        help="Optional description for release message")

    args = parser.parse_args()
    update_platy_browser(args.update_cell_segmentation,
                         args.update_nucleus_segmentation,
                         args.update_cell_tables,
                         args.update_nucleus_tables,
                         args.description)
