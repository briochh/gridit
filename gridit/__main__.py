"""Command-line interface for grid package."""
import sys
from importlib.util import find_spec
from pathlib import Path
from textwrap import dedent

import numpy as np

try:
    import rasterio
    from rasterio._err import CPLE_BaseError as RasterioCPLE_BaseError
    from rasterio.errors import RasterioError
except ModuleNotFoundError:
    rasterio = None

try:
    import fiona
    from fiona._err import CPLE_BaseError as FionaCPLE_BaseError
    from fiona.errors import FionaError
except ModuleNotFoundError:
    fiona = None

try:
    import matplotlib.pyplot as plt
except ModuleNotFoundError:
    plt = None

from . import cli, GridPolyConv
from .display import print_array
from .logger import get_logger

has_flopy = find_spec("flopy") is not None
has_netcdf4 = find_spec("netCDF4") is not None


def cli_main():
    """Command-line interface for the gridit package.

    To use:
    $ gridit -h
    """
    import argparse
    from tempfile import gettempdir

    if sys.platform.startswith("win"):
        cl = "^"  # assume cmd.exe
    else:
        cl = "\\"  # assume bash-like
    tmpdir = Path(gettempdir())
    mana_shp = Path("tests/data/Mana_polygons.shp")
    examples = f"""\
Examples:

  Grid from vector:
  $ gridit --grid-from-vector {mana_shp} --resolution 10

  Array from vector, write PNG image:
  $ gridit --grid-from-vector {mana_shp} --resolution 10 {cl}
      --array-from-vector {mana_shp} {cl}
      --array-from-vector-attribute=K_m_d {cl}
      --write-image {tmpdir / "Mana_Kmd.png"}

  Array from raster, write GeoTIFF raster:
  $ gridit --grid-from-vector {mana_shp} --resolution 10 {cl}
      --array-from-raster {Path("tests/data/Mana.tif")} {cl}
      --write-raster {tmpdir / "Mana_10m.tif"}
"""
    if has_netcdf4:
        waitaku2 = Path("tests/data/waitaku2")
        examples += f"""\

  Array from netCDF, write text array files:
  $ gridit --grid-from-vector {waitaku2}.shp --resolution 250 {cl}
      --array-from-vector {waitaku2}.shp {cl}
      --array-from-vector-attribute rid {cl}
      --array-from-netcdf {waitaku2}.nc:rid:__xarray_dataarray_variable__ {cl}
      --time-stats "quantile(0.75),max" {cl}
      --write-text {tmpdir / "waitaku2_cat.ref"}
"""  # noqa
    if has_flopy:
        examples += f"""\

  Array from MODFLOW, write text array file:
  $ gridit --grid-from-modflow {Path("tests/data/modflow/mfsim.nam")}:h6 {cl}
      --array-from-vector {waitaku2}.shp {cl}
      --array-from-vector-attribute rid {cl}
      --write-text {tmpdir / "waitaku2_rid.txt"}
"""
    parser = argparse.ArgumentParser(
        prog=__package__, description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=examples)

    parser.add_argument(
        "--logger", metavar="LEVEL", default="INFO",
        help="Logger level, default INFO. Use WARNING to show fewer messages.")

    cli.add_grid_parser_arguments(parser)

    if rasterio:
        array_from_raster_group = parser.add_argument_group(
            "Array from raster")
        array_from_raster_group.add_argument(
            "--array-from-raster", metavar="FILE[:BIDX]",
            help="Source raster file, and optional band index "
            "(default 1 for first band)."
        )
        array_from_raster_group.add_argument(
            "--array-from-raster-resampling", metavar="SMP",
            help="Raster resampling method, default None to "
                 "automatically choose. Use one of: " +
                 ", ".join(rasterio.enums.Resampling.__members__.keys())
        )
    else:
        parser.add_argument_group(
            "Array from raster", "rasterio not installed")

    if has_netcdf4 and fiona:
        array_from_netcdf_group = parser.add_argument_group(
            "Array from catchment netCDF")
        array_from_netcdf_group.add_argument(
            "--array-from-netcdf", metavar="F:I:V",
            help=dedent("""\
                Source netCDF of catchment values supplied in the format:
                'file.nc:idx_name:var_name' where 'file.nc' is a path to a
                netCDF file, 'idx_name' is the variable name with the polygon
                index, and 'var_name' is the name of the variable for values.
                If the variable has a time dimension, it is reduced by
                evaluating time statistics, with default 'mean'."""))
        array_from_netcdf_group.add_argument(
            "--time-stats", metavar="TYPE", default="mean",
            help=dedent("""\
                Compute time-statistics along time dimension.
                Default "mean" evaluates the mean values. Other types may
                include "min", "median", "max", "quantile(N)" where N is a
                real value between 0.0 and 1.0. An optional time-window can
                specify a range of months or specify hydrlogic years, which
                modifies "min", "median" and "max" calculations to find
                years with "lowest", "middle" or "highest" total values.
                E.g. "Jul-Jun:min" will find the NZ water year with lowest
                values."""))
    else:
        parser.add_argument_group(
            "Array from catchment netCDF",
            "netCDF4 and/or fiona not installed")

    if fiona:
        array_from_vector_group = parser.add_argument_group(
            "Array from vector")
        array_from_vector_group.add_argument(
            "--array-from-vector", metavar="FILE[:LAYER]",
            help="Source vector file. For multilayer datasources, use the "
            "format 'datasource:layer'"
        )
        array_from_vector_group.add_argument(
            "--array-from-vector-attribute", metavar="NAME",
            help="Name of attribute to rasterize. If None, a boolean result "
                 "where polygon features are located is returned."
        )
        array_from_vector_group.add_argument(
            "--array-from-vector-fill", metavar="FILL", default=0,
            help="Fill value, only used where polygon does not cover unmasked "
                 "grid. Default fill value is 0."
        )
        array_from_vector_group.add_argument(
            "--array-from-vector-refine",
            metavar="INT", type=int, default=5,
            help="If greater than 1, refine each dimension by a factor as a "
                 "pre-processing step to approximate more details from the "
                 "vector file to the gridded result. Default 5."
        )
        array_from_vector_group.add_argument(
            "--array-from-vector-max-levels",
            metavar="INT", type=int, default=5,
            help="If refine is greater than 1, set a maximum number "
                 "of refine levels. Default 5."
        )
    else:
        parser.add_argument_group(
            "Array from vector", "fiona not installed")

    write_output_group = parser.add_argument_group("Write outputs")
    write_output_group.add_argument(
        "--turn-off-print-array", action="store_true",
        help="Disable printing array image to console")
    write_output_group.add_argument(
        "--write-image", metavar="FILE",
        help="Write array image file, e.g., 'output.png'")
    write_output_group.add_argument(
        "--write-raster", metavar="FILE",
        help="Write array raster file, e.g., 'output.tif'")
    write_output_group.add_argument(
        "--write-text", metavar="FILE[:FMT]",
        help="Write array text file, e.g. 'output.txt'. An optional C format "
        "specification may follow after ':', e.g. 'output.txt:%%12.7E'. "
        "The default is '%%s' for free format.")

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    logger = get_logger(__package__, args.logger)

    def error(msg, name="", show_usage=False, exit=1):
        if show_usage:
            parser.print_usage(sys.stderr)
        m = ""
        if name:
            m = "--" + name.replace("_", "-") + ": "
        logger.error(m + str(msg))
        if exit:
            sys.exit(exit)

    def write_output(ar, part=None):
        orig_part = part
        if part is not None:
            part = part.replace(" ", "_")
        if ar.dtype == np.float64:
            ar = ar.astype(np.float32)
        if not args.turn_off_print_array:
            print_array(ar, logger=logger)
        if args.write_image is not None:
            # Write matplotlib images
            fname = Path(args.write_image)
            title = fname.stem
            if part:
                fname = fname.parent / f"{fname.stem}_{part}{fname.suffix}"
                title += " " + orig_part
            fig, ax = plt.subplots(nrows=1, ncols=1)
            im = ax.imshow(ar)
            ax.set_title(title)
            fig = plt.gcf()
            fig.colorbar(im)
            logger.info("writing image: %s", fname)
            try:
                fig.savefig(fname)
            except OSError as err:
                error(f"cannot write image: {err}", exit=0)
        if args.write_raster is not None:
            fname = Path(args.write_raster)
            if part:
                fname = fname.parent / f"{fname.stem}_{part}{fname.suffix}"
            try:
                grid.write_raster(ar, fname)
            except (RasterioCPLE_BaseError, RasterioError) as err:
                error(f"cannot write raster: {err}", exit=1)
        if args.write_text is not None:
            fname = args.write_text
            fmt = "%s"
            if ":" in args.write_text:
                fname, fmt = fname.split(":", 1)
            if part:
                fname = Path(fname)
                fname = fname.parent / f"{fname.stem}_{part}{fname.suffix}"
            logger.info("writing text (%s): %s", fmt, fname)
            try:
                np.savetxt(fname, ar, fmt)
            except OSError as err:
                error(f"cannot write text: {err}", exit=0)

    # Process grid options

    try:
        grid, mask = cli.process_grid_options(args, logger)
    except ValueError as err:
        error(err, show_usage=True)
    except (ModuleNotFoundError, OSError) as err:
        error(err, show_usage=False)

    logger.info("%s", grid)
    logger.info("has mask: %s", mask is not None)

    # Process array from * options

    array = None
    if getattr(args, "array_from_raster", None):
        fname = args.array_from_raster
        bidx = 1
        if ":" in fname:
            fname, bidx = fname.split(":", 1)
            try:
                bidx = int(bidx)
            except ValueError as err:
                error(err, "array_from_raster", True)
        try:
            array = grid.array_from_raster(
                fname=fname, bidx=bidx,
                resampling=args.array_from_raster_resampling,
            )
        except (RasterioCPLE_BaseError, RasterioError) as err:
            error(err, "array_from_raster", exit=1)
        write_output(array)
        logger.info("done")
        return

    if getattr(args, "array_from_netcdf", None):
        name_nc = "array_from_netcdf"
        nc_arg = getattr(args, name_nc)
        try:
            nc_fname, idx_name, var_name = cli.process_nc_arg(nc_arg)
        except ValueError as err:
            error(err, name_nc, show_usage=True)

        vector_fname = getattr(args, "array_from_vector", None)
        layer = None
        attr_name = "array_from_vector_attribute"
        attr = getattr(args, attr_name, None)
        if vector_fname is None or attr is None:
            error("missing --array-from-vector and/or "
                  "--array-from-vector-attribute, which are required "
                  "to provide spatial distributions of catchment polygons, "
                  "and the common index attribute name", name_nc)
        if ":" in vector_fname:
            vector_fname, layer = vector_fname.split(":", 1)

        gpc = GridPolyConv.from_grid_vector(
            grid, vector_fname, attr, layer=layer,
            refine=args.array_from_vector_refine,
            max_levels=args.array_from_vector_max_levels)

        fill = args.array_from_vector_fill
        ar_d = gpc.array_from_netcdf(
            nc_fname, idx_name, var_name,
            time_stats=args.time_stats, fill=fill, enforce1d=True)
        for key, array in ar_d.items():
            logger.info("time stats: %s", key)
            if array.ndim == 3:
                idxs = [0]
                if array.shape[0] > 1:
                    idxs.append(array.shape[0] - 1)
                for idx in idxs:
                    logger.info("array index: %s", idx)
                    write_output(array[idx], part=f"{key} {idx}")
            else:
                write_output(array, part=key)
        logger.info("done")
        return

    if getattr(args, "array_from_vector", None):
        fname = args.array_from_vector
        layer = None
        if ":" in fname:
            fname, layer = fname.split(":", 1)
        try:
            array = grid.array_from_vector(
                fname=fname, layer=layer,
                attribute=args.array_from_vector_attribute,
                fill=args.array_from_vector_fill,
                refine=args.array_from_vector_refine,
            )
            write_output(array)
            logger.info("done")
            return
        except (FionaCPLE_BaseError, FionaError) as err:
            error(err, "array_from_vector", exit=1)

    logger.info("done")


if __name__ == "__main__":
    cli_main()
