"""Grid-polygon conversion."""
__all__ = ["GridPolyConv"]

import calendar
import numpy as np
import os
import pickle
import re
from collections import Counter
from pathlib import Path

from .display import shorten
from .grid import Grid
from .logger import get_logger, disable_logger
from .spatial import is_same_crs


def month_number(txt):
    """Return month number from 1 to 12 from input txt."""
    if txt == "":
        raise ValueError("month input cannot be empty")
    Txt = txt.title()
    month_abbr = list(calendar.month_abbr)
    if Txt in month_abbr:
        return month_abbr.index(Txt)
    month_name = list(calendar.month_name)
    if Txt in month_name:
        return month_name.index(Txt)
    raise ValueError(f"cannot evaluate month with {txt!r}")


class GridPolyConv:
    """Create a GridPolyConv object.

    Parameters
    ----------
    poly_idx : tuple
        Tuple of the polygon index.
    idx_ar : array_like
        Index array, where values are betwen 0 and len(poly_idx), where 0 is
        nodata. Dimensions are 2D (nrow, ncol) or 3D (nrow, ncol, nlev) if
        more than one level of digitization is used.
    ar_count : array_like, optional
        If one or more levels of digitization is used, this is a 3D integer
        array with the same dimensions as idx_ar, and represents the number
        of counts with idx_ar values.
    logger : logging.Logger, optional
        Logger to show messages.

    Attributes
    ----------
    weight : array_like or None
        If idx_ar is 2D, this attribute will be None. Otherwise, it is
        a 3D array of floats with the same shape as idx_ar, generated by
        ar_count divided by ``ar_count.sum(0)`` where non-zero.
    mask : array_like
        2D array used to set mask for outputs.
    """

    def __init__(self, poly_idx, idx_ar, ar_count=None, logger=None):
        if logger is None:
            self.logger = get_logger(__package__)
        else:
            self.logger = logger
        if not isinstance(poly_idx, tuple):
            try:
                poly_idx = tuple(poly_idx)
            except Exception:
                raise ValueError(
                    "poly_idx must be a tuple or list-like; "
                    f"found {type(poly_idx)!r}")
        if len(poly_idx) != len(set(poly_idx)):
            raise ValueError("poly_idx values are not unique")
        self.poly_idx = poly_idx
        # self.idx_d = dict(enumerate(poly_idx, 1))
        if not isinstance(idx_ar, np.ndarray):
            raise ValueError(
                f"idx_ar must be a numpy array; found {type(idx_ar)!r}")
        elif not np.issubdtype(idx_ar.dtype, np.integer):
            raise ValueError(
                f"idx_ar dtype must integer-based; found {idx_ar.dtype!r}")
        self.idx_ar = idx_ar.copy()
        self.idx_ar.flags.writeable = False

        # if min(poly_idx) > 0:
        #    self.nodata = 0
        # else:
        #    self.nodata = min(poly_idx) - 1
        # poly_idx_dtype = np.min_scalar_type(self.poly_idx)
        # self.poly_idx_ar = np.vectorize(
        #    self.idx_d.get, otypes=[poly_idx_dtype])(self.idx_ar, self.nodata)
        # self.poly_idx_ar.flags.writeable = False

        if idx_ar.ndim == 2:
            if ar_count is not None:
                self.logger.error("ignoring ar_count, since idx_ar is 2D")
            self.ar_count = None
            self.weight = None
            self.mask = idx_ar == 0
        elif idx_ar.ndim == 3:
            if ar_count is None:
                raise ValueError("ar_count must be specified if idx_ar is 3D")
            elif not isinstance(ar_count, np.ndarray):
                raise ValueError(
                    "ar_count must be a numpy array; "
                    f"found {type(ar_count)!r}")
            elif not np.issubdtype(ar_count.dtype, np.integer):
                raise ValueError(
                    "ar_count dtype must integer-based; "
                    f"found {ar_count.dtype!r}")
            elif ar_count.shape != idx_ar.shape:
                raise ValueError(
                    "ar_count shape must match idx_ar; "
                    f"found {ar_count.shape}")
            self.ar_count = ar_count.copy()
            self.ar_count.flags.writeable = False
            denominator = ar_count.sum(0)
            if (denominator != 0).all():
                self.weight = ar_count / denominator
            else:
                denominator = np.broadcast_arrays(ar_count, denominator)[1]
                nzdm = denominator != 0
                self.weight = np.zeros(idx_ar.shape)
                self.weight[nzdm] = ar_count[nzdm] / denominator[nzdm]
            self.weight.flags.writeable = False
            self.mask = idx_ar[0] == 0
        else:
            raise ValueError(
                f"idx_ar ndim must be 2 or 3; found {idx_ar.ndim}")
        self.mask.flags.writeable = False

    def __eq__(self, other):
        """Return True if objects are equal."""
        if self.__class__.__name__ != other.__class__.__name__:
            return False
        elif self.poly_idx != getattr(other, "poly_idx", None):
            return False
        try:
            np.testing.assert_array_equal(
                self.idx_ar, getattr(other, "idx_ar", None))
            return True
        except AssertionError:
            return False
        if self.ar_count is not None:
            try:
                np.testing.assert_array_equal(
                    self.ar_count, getattr(other, "ar_count", None))
                return True
            except AssertionError:
                return False
        elif getattr(other, "ar_count", None) is not None:
            return False
        return True

    def __iter__(self):
        """Return object datasets with an iterator."""
        yield "poly_idx", self.poly_idx
        yield "idx_ar", self.idx_ar
        yield "ar_count", self.ar_count

    def __getstate__(self):
        """Serialize object attributes for pickle dumps."""
        return dict(self)

    def __setstate__(self, state):
        """Set object attributes from pickle loads."""
        self.__init__(**state)

    @classmethod
    def from_grid_vector(
            cls, grid, fname: str, attribute: str, *, layer=None,
            refine: int = 5, max_levels: int = 5, caching: int = 1,
            logger=None):
        """Create grid-polygon conversion from a polygon vector file.

        Parameters
        ----------
        grid : Grid
            A Grid instance.
        fname : str
            Path to vector file with polygons.
        attribute : str
            Name of vector attribute to be used for poly_idx.
        layer : int or str, default None
            The integer index or name of a layer in a multi-layer dataset.
        refine : int, default 5
            If greater than 1, refine each dimension by a factor as a
            pre-processing step to gather more information in each grid cell.
        max_levels : int, default 5
            Maximum number of digitization levels, as required. Use 1 for
            fastest methods that require the least amount of memory. This
            option only works when refine is greater than 1.
        caching : int, default 1
            Caching level, 0 is none (don't find, don't store), 1 is
            standard level based on grid, poly_idx values (of any order),
            and refine. Caching level 2 also hashes geometry data,
            which may take more time to process.
        logger : logging.Logger, optional
            Logger to show messages.

        Raises
        ------
        ModuleNotFoundError
            If fiona and/or rasterio is not installed.

        """
        if logger is None:
            logger = get_logger(__package__)
        logger.info("creating grid-polygon conversion from vector file")
        try:
            import fiona
            from affine import Affine
            from rasterio import features
            from rasterio.dtypes import get_minimum_dtype
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "from_grid_vector requires fiona and rasterio")
        if not isinstance(grid, Grid):
            raise ValueError("grid must be an instance of Grid")
        grid_transform = grid.transform
        grid_crs = grid.projection
        if not isinstance(refine, int):
            raise ValueError("refine must be int")
        elif refine < 1:
            raise ValueError("refine must be >= 1")
        use_refine = refine > 1
        if use_refine:
            if not isinstance(max_levels, int):
                raise ValueError("max_levels must be int")
            elif max_levels < 1:
                raise ValueError("max_levels must be >= 1")
        logger.info("reading polygon: %s", fname)
        if layer is None:
            layers = fiona.listlayers(fname)
            if len(layers) > 1:
                logger.warning(
                    "choosing the first of %d layers: %s", len(layers), layers)
                layer = layers[0]
        with fiona.open(fname, "r", layer=layer) as ds:
            if "Polygon" not in ds.schema["geometry"]:
                logger.error(
                    "expected [Multi]Polygon, found %s", ds.schema["geometry"])
            ds_crs = ds.crs_wkt
            do_transform = False
            if not grid_crs:
                grid_crs = ds_crs
                logger.info(
                    "assuming same projection: %s", shorten(grid_crs, 60))
            elif is_same_crs(grid_crs, ds_crs):
                grid_crs = ds_crs
                logger.info(
                    "same projection: %s", shorten(grid_crs, 60))
            else:
                do_transform = True
                from fiona.transform import transform_geom
                from shapely.geometry import box, mapping, shape
                logger.info(
                    "geometries will be transformed from %s to %s",
                    shorten(ds_crs, 60), shorten(grid_crs, 60))
            attributes = list(ds.schema["properties"].keys())
            if attribute not in attributes:
                raise KeyError(
                    f"could not find '{attribute}' in {attributes}")
            geoms = []
            vals = []
            grid_bounds = grid.bounds
            if do_transform:
                grid_box = box(*grid_bounds)
                # TODO: does this make sense?
                buf = grid.resolution * np.average(grid.shape) * 0.15
                grid_box_t = shape(transform_geom(
                    grid_crs, ds_crs,
                    mapping(grid_box.buffer(buf)))).buffer(buf)
                kwargs = {"bbox": grid_box_t.bounds}
                logger.info(
                    "transforming features in bbox %s", grid_box_t.bounds)
                for _, feat in ds.items(**kwargs):
                    geom = transform_geom(ds_crs, grid_crs, feat["geometry"])
                    if not grid_box.intersects(shape(geom)):
                        continue
                    val = feat["properties"][attribute]
                    geoms.append(geom)
                    vals.append(val)
            else:
                for _, feat in ds.items(bbox=grid_bounds):
                    geom = feat["geometry"]
                    val = feat["properties"][attribute]
                    geoms.append(geom)
                    vals.append(val)
        if not vals:
            raise ValueError("no features were found in grid extent")
            raise ValueError(f"{attribute!r} values are not unique")
        # Generate mapping with sorted values and sequence starting from 1
        nodata = 0
        vals_d = dict(enumerate(sorted(set(vals)), 1))
        poly_idx = list(vals_d.values())
        idx_vals = {v: k for k, v in vals_d.items()}
        idxs = list(map(idx_vals.get, vals))
        geoms_idxs = list(zip(geoms, idxs))
        if caching:

            def find_cache(dirname, fname):
                if not dirname.is_dir():
                    return None
                list_dir = [
                    f.name for f in Path(dirname).iterdir()
                    if f.is_file() and f.name[0] == "c"
                    and f.suffix == ".gpc"]
                if len(list_dir) == 0:
                    return None
                elif fname in list_dir:
                    return dirname / fname
                elif caching == 1:
                    prefix = fname[:9]
                    part_list_dir = [f for f in list_dir if f[:9] == prefix]
                    if part_list_dir:
                        # there might be more than one!
                        return dirname / part_list_dir[0]

            if caching == 1:
                args = (grid, poly_idx, refine)
            elif caching == 2:
                args = (grid, poly_idx, refine, geoms_idxs)
            else:
                raise ValueError("caching must be 0, 1 or 2")
            cache_fname = cls.generate_cached_fname(*args)
            cache_grid_dir = os.environ.get("GRID_CACHE_DIR")
            if cache_grid_dir:
                cache_grid_dir = Path(cache_grid_dir)
                if not cache_grid_dir.is_dir():
                    raise OSError("GRID_CACHE_DIR is not a directory")
                cache_path = find_cache(cache_grid_dir, cache_fname)
            else:
                # First try in the directory with the vector file
                cache_path = find_cache(Path(fname).parent, cache_fname)
                if cache_path is None:
                    # Next attempt is the current directory
                    cache_path = find_cache(Path("."), cache_fname)
            if cache_path is not None:
                logger.info("reading cached file: %s", cache_path)
                try:
                    with open(cache_path, "rb") as f:
                        obj = pickle.load(f)
                    obj.logger = logger
                    return obj
                except Exception as e:
                    logger.error("cannot read cache: %s", e)
                    os.remove(cache_path)

        idx_dtype = get_minimum_dtype(max(idxs))
        msg = "rasterizing indexed %r to %s array"
        msg_args = [attribute, idx_dtype]
        if use_refine:
            msg += " with refine factor %d"
            msg_args.append(refine)
            logger.info(msg, *tuple(msg_args))
            fine_shape = tuple(n * refine for n in grid.shape)
            fine_transform = grid_transform * Affine.scale(1. / refine)
            idx_ar = features.rasterize(
                geoms_idxs, fine_shape, transform=fine_transform,
                fill=nodata, dtype=idx_dtype, all_touched=False)
        else:
            msg += " with no refine factor"
            logger.info(msg, *tuple(msg_args))
            idx_ar = features.rasterize(
                geoms_idxs, grid.shape, transform=grid_transform,
                fill=nodata, dtype=idx_dtype, all_touched=False)
        uaridx = np.unique(idx_ar)
        if nodata in uaridx:
            uaridx = np.delete(uaridx, nodata)
        uaridx_s = set(uaridx)
        idxs_s = set(idxs)
        if uaridx_s == idxs_s:
            logger.info("all %d polygon indexes were rasterized", len(idxs_s))
        elif uaridx_s.issubset(idxs_s):
            logger.info(
                "subset %d of %d polygon indexes were rasterized",
                len(uaridx_s), len(geoms))
            missing_idx = idxs_s.difference(uaridx_s)
            if len(missing_idx) < 20:
                plr = "" if len(missing_idx) == 1 else "s"
                logger.info(
                    "missing idx value%s: %s or %r value%s: %s",
                    plr, sorted(missing_idx), attribute, plr,
                    sorted(map(vals_d.get, missing_idx)))
            else:
                logger.info("missing %s polygon indexes", len(missing_idx))
        else:
            logger.error(
                "from %d polygons, none were rasterized", len(geoms))

        ar_count = None
        if use_refine:
            from rasterio.enums import Resampling
            from rasterio.warp import reproject

            # variables to zoom
            fx, fy = fine_shape
            fX, fY = np.ogrid[0:fx, 0:fy]
            cX = fX//refine
            cY = fY//refine
            if refine >= 16:
                frac_dtype = np.uint16
            else:
                frac_dtype = np.uint8
            # total counts of non-zero idx_ar per cell
            # total_ar_count = np.zeros(grid.shape, dtype=frac_dtype)
            # _ = reproject(
            #    (idx_ar != 0).astype(np.uint8), total_ar_count,
            #    src_transform=fine_transform, dst_transform=grid_transform,
            #    src_crs=ds_crs, dst_crs=ds_crs,
            #    src_nodata=0, dst_nodata=0,
            #    resampling=Resampling.sum)
            idx_l = []
            ar_count_l = []
            for lev in range(max_levels):
                logger.debug("evaluating refine level %s", lev)
                idx2d = np.zeros(grid.shape, dtype=idx_dtype)
                _ = reproject(
                    idx_ar, idx2d,
                    src_transform=fine_transform, dst_transform=grid_transform,
                    src_crs=ds_crs, dst_crs=ds_crs,
                    src_nodata=0, dst_nodata=0,
                    resampling=Resampling.mode)
                idx_l.append(idx2d)
                idx2d_fine = idx2d[cX, cY]
                sel2d = np.logical_and(
                    idx_ar == idx2d_fine,
                    idx_ar != 0)
                count2d = np.zeros(grid.shape, dtype=frac_dtype)
                _ = reproject(
                    (sel2d).astype(np.uint8), count2d,
                    src_transform=fine_transform, dst_transform=grid_transform,
                    src_crs=ds_crs, dst_crs=ds_crs,
                    src_nodata=0, dst_nodata=0,
                    resampling=Resampling.sum)
                ar_count_l.append(count2d)
                # Remove previous values
                idx_ar[idx_ar == idx2d_fine] = 0
                if not idx_ar.any():
                    logger.debug("no more remaining refine levels are needed")
                    break
            else:
                logger.debug(
                    "more refine levels could be requested "
                    "by increasing max_levels (currently %s)", max_levels)
            # Make 3D arrays
            idx_ar = np.stack(idx_l)
            ar_count = np.stack(ar_count_l)
        obj = cls(poly_idx, idx_ar, ar_count, logger)

        if caching:
            def write_cache(dirname):
                """Return "tryagain" bool."""
                cache_path = dirname / cache_fname
                try:
                    with open(cache_path, "wb") as f:
                        pickle.dump(obj, f, protocol=4)
                    obj.logger.info("wrote cache file: %s", cache_path)
                    return False
                except pickle.PicklingError as e:
                    obj.logger.error(
                        "could not create cache file: %s", cache_path)
                    raise e
                except Exception as e:
                    obj.logger.error(
                        "could not create cache file: %s\n%s", cache_path, e)
                    return True

            if cache_grid_dir:
                _ = write_cache(cache_grid_dir)
            else:
                tryagain = write_cache(Path(fname).parent)
                if tryagain:
                    _ = write_cache(Path("."))
        return obj

    @staticmethod
    def generate_cached_fname(grid, vals_d, refine, shapes=None):
        """Generate a cached filename."""
        from hashlib import md5
        prefix = "c"
        suffix = ".gpc"
        pt1 = md5(
            str(grid).encode() +
            str(vals_d).encode() +
            str(refine).encode()).hexdigest()[:8]
        if shapes is None:
            return prefix + pt1 + suffix
        pt2 = md5(
            str(list(sorted(shapes, key=lambda item: item[1]))).encode()
            ).hexdigest()[:8]
        return prefix + pt1 + pt2 + suffix

    def to_pickle(self, path, protocol=4):
        """Pickle (serialize) object to file.

        Parameters
        ----------
        path : str
            File path where the pickled object will be stored.
        protocol : int, default 4
            Default 4 was introduced for Python 3.4.
        """
        with open(path, "wb") as f:
            pickle.dump(self, f, protocol=protocol)

    @staticmethod
    def from_pickle(fname: str):
        """Unpickle object from a file."""
        with open(fname, "rb") as f:
            obj = pickle.load(f)
        return obj

    def array_from_values(self, index, values, fill=0, enforce1d=False):
        """Generate 2D or 3D array from 1D or 2D values.

        Parameters
        ----------
        index : list
            List of polygon index.
        values : array_like
            Numpy array with 1 or 2 dimensions, with index on last dimension.
        fill : float or int, default 0
            Fill value, only used where polygon does not cover unmasked grid.
        enforce1d : bool, default False
            If True, raise ValueError if evaluated variable does not have
            one dimension.

        Returns
        -------
        np.ma.array
            If values is 1D (npoly,), return a 2D array with (nrow, ncol).
            If values is 2D (extra, npoly), return a 3D array with
            (extra, nrow, ncol).
        """
        if not isinstance(index, list):
            try:
                index = list(index)
            except Exception:
                raise ValueError("expected index be a list-like")
        if not isinstance(values, np.ndarray):
            values = np.array(values)
        if not hasattr(values, "ndim"):
            raise ValueError("expected values be array-like")
        elif values.ndim not in (1, 2):
            raise ValueError("expected values to have 1 or 2 dimensions")
        elif len(index) != values.shape[-1]:
            raise ValueError(
                "length of last dimension of values "
                "does not match index length")
        self.logger.info(
            "reading array from values with shape %s", values.shape)
        if enforce1d and values.ndim != 1:
            raise ValueError("values must have one dimension")
        index_s = set(index)
        poly_idx_s = set(self.poly_idx)
        poly_idx_l = list(self.poly_idx)
        if index_s.isdisjoint(poly_idx_s):
            raise ValueError("index is disjoint from poly_idx")
        elif not index_s.issuperset(poly_idx_s):
            raise ValueError("index is not a superset of poly_idx")
        if index != poly_idx_l:
            # subset and/or re-order values to match poly_idx
            order = []
            for idx in self.poly_idx:
                if idx in index_s:
                    order.append(index.index(idx))
            if values.ndim == 1:
                values = values[order]
            else:  # ndim == 2
                values = values[:, order]
            self.logger.info(
                "subset/re-ordered values to shape %s", values.shape)
        # ar index 0 is fill, replace index values
        if values.ndim == 1:
            values = np.insert(values, 0, fill)
            ar_values = values[self.idx_ar]
            if self.weight is None:
                ar = np.ma.array(ar_values)
            else:
                ar = np.ma.array((ar_values * self.weight).sum(0))
        else:  # ndim == 2
            values = np.insert(values, 0, fill, axis=1)
            ar_l = []
            for vals in values:
                ar_values = vals[self.idx_ar]
                if self.weight is None:
                    ar_l.append(ar_values)
                else:
                    ar_l.append((ar_values * self.weight).sum(0))
            ar = np.ma.stack(ar_l)
        if fill and self.weight is not None:
            weight2d = np.clip(self.weight.sum(0), 0.0, 1.0)
            outside = (1.0 - weight2d) * fill
            ar += outside
        # don't modify ar.fill_value
        ar.mask = self.mask
        return ar

    def array_from_netcdf(
            self, fname: str, idx_name: str, var_name: str, *, xidx=None,
            time_stats: str = "mean", fill=0, enforce1d: bool = False):
        """Return array from a netCDF source with polygon index.

        Parameters
        ----------
        fname : str or xarray.Dataset
            Source netCDF file. This can also be an xarray Dataset object.
        idx_name : str
            Variable name with the polygon index.
        var_name : str
            Name of the variable for values. If the dataset has a 'time'
            dimension, time statistics are evaluated for the  catchment values.
        xidx : int or None (default)
            If not None, index an extra dimension, such as an ensemble run,
            using a base-0 index number.
        time_stats : str or None, default "mean"
            Perform statistics on time dimension, if present. The optional
            first part is a time-window, which can be "annual" or a month
            range, e.g., "Jan-Mar", which selects the months to be averaged,
            grouped by each year. "July-June" can define water years.
            The second required part is one or more time statistics to perform,
            which may include:
                - "mean" calculate mean values
                - "median" for median values
                - "min" for minimum values
                - "max" for maxiumum values
                - "quantile(N)" where N is a real value between 0 and 1.
            When a time-window is specified, some of these calculations are
            done differently, such that they take the year-mean values:
                - "min" for the year with the total minimum value
                - "median" for the year with the total "middle" value
                - "max" for the year with the total maximum value
                - Other statistics "mean" and "quantile(N)" are not changed.
            Examples:
                - "Jan-Mar:mean": evaluate mean values from January to March
                - "Jul-Jun:min,max": get the NZ water year with the lowest
                    and highest mean monthly values
            Default behaviour will perform "mean" catchment values.
        fill : float or int, default 0
            Fill value, only used where polygon does not cover unmasked grid.
        enforce1d : bool, default False
            If True, raise ValueError if evaluated variable does not have
            one dimension.

        Returns
        -------
        dict
            Key is time_stat and value is np.ma.array

        Raises
        ------
        ModuleNotFoundError
            If xarray is not installed.
        AttributeError
            If variables cannot be found in netCDF file.
        ValueError
            If there are errors with inputs or dimensions.
        """
        try:
            import xarray
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "array_from_netcdf requires xarray")

        if time_stats is not None:
            if ":" in time_stats:
                if time_stats.count(":") != 1:
                    raise ValueError("expected one ':' in time stats")
                time_window, stats_types_str = time_stats.split(":")
                time_stats_l = stats_types_str.split(",")
                if time_window == "annual":  # support other keywords?
                    start_month = 1
                    end_month = 12
                elif "-" in time_window:
                    if time_window.count("-") != 1:
                        raise ValueError(
                            "too many '-' for time_window: {time_window}")
                    start_month, end_month = time_window.split("-")
                    start_month = month_number(start_month)
                    end_month = month_number(end_month)
                else:
                    raise ValueError(
                        f"time stats window {time_window!r} "
                        "not supported; use 'annual' or a month range")
            else:
                time_window = None
                time_stats_l = time_stats.split(",")
                start_month = 1
                end_month = 12

        fname_is_dataset = isinstance(fname, xarray.Dataset)
        if fname_is_dataset:
            ds = fname
        else:
            self.logger.info("reading array from netCDF file %s", fname)
            ds = xarray.open_dataset(fname, decode_coords=False)

        avail = list(ds.variables.keys())
        if var_name not in avail:
            raise AttributeError(
                f"cannot find '{var_name}' in variables: {avail}")
        elif idx_name not in avail:
            raise AttributeError(
                f"cannot find '{idx_name}' in variables: {avail}")
        if idx_name not in ds.coords:
            new_coords = []
            if "time" in ds.coords:
                new_coords.append("time")
            new_coords.append(idx_name)
            ds = ds.set_coords(new_coords).squeeze()
        idx_dims = ds[idx_name].dims
        if len(idx_dims) != 1:
            raise ValueError(f"expected 1-d {idx_name} index dimension")
        var = ds[var_name]
        self.logger.info(
            "found variable %r with %d dims/shape: %s", var_name, var.ndim,
            ", ".join(f"{k}: {v}" for k, v in zip(var.dims, var.shape)))
        if idx_name not in var.dims:
            var = var.swap_dims({idx_dims[0]: idx_name})
        # Handle extra dimension/index
        rem_dims = sorted(set(var.dims).difference({"time", idx_name}))
        if len(rem_dims) == 1:
            rem_dim = rem_dims[0]
            if xidx is None:
                xidx = 0
                self.logger.warning(
                    "dataset has extra dimension %r that should be indexed "
                    "using xidx; choosing index %s from size %s",
                    rem_dim, xidx, ds.dims[rem_dim])
            else:
                self.logger.info(
                    "selecting xidx %s from %r with size %s",
                    xidx, rem_dim, ds.dims[rem_dim])
            var = var.loc[{rem_dim: xidx}]
        elif len(rem_dims) == 0 and xidx is not None:
            self.logger.warning(
                "xidx %s is ignored, no extra index found", xidx)
        # variable index dimension must be last
        if var.ndim > 1 and var.dims[-1] != idx_name:
            var = var.transpose(..., idx_name)
        # Select the values from the catchments
        self.logger.debug(
            "loading data from %s catchments ...", len(self.poly_idx))
        var = var.sel({idx_name: list(self.poly_idx)}).load()
        self.logger.debug("... done")

        if not fname_is_dataset:
            ds.close()

        # Process data, and return a dict of np.ma.array
        ret = {}
        idx = var[idx_name].values
        if time_stats is not None and "time" in var.dims:
            self.logger.info(
                "determining time stats %r of %r along time dimension",
                time_stats, var_name)
            time = var["time"]
            month = np.array(time.dt.month)
            num_months = end_month - start_month + 1
            if (num_months % 12) == 0:
                # select all months
                month_sel = np.ones_like(month).astype(bool)
                self.logger.info(
                    "performing statistics along full time dimension")
            else:
                if num_months < 1:
                    num_months += 12
                    month_sel = (
                        (month >= start_month) | (month <= end_month))
                else:
                    month_sel = (
                        (month >= start_month) & (month <= end_month))
                self.logger.info(
                    "performing statistics with a %d-month window, "
                    "starting in %s",
                    num_months, calendar.month_name[start_month])
            has_partial_month_sel = not month_sel.all()
            if has_partial_month_sel:
                self.logger.debug(
                    "month counts: %s",
                    dict(sorted(Counter(month[month_sel]).items())))
            if (set(time_stats_l).intersection(["min", "max", "median"]) and
                    time_window):
                # year totals are only needed for min, max, median
                year = time.dt.year
                if start_month > end_month:
                    year[month < start_month] -= 1
                    yp = year.to_pandas()
                    year = year.astype(str)
                    year[:] = yp.astype(str) + "-" + (yp + 1).astype(str)
                    bogus = "bogus"
                else:
                    bogus = -1
                if has_partial_month_sel:
                    # use a bogus year to remove from mean
                    year[~month_sel] = bogus
                    var_mean = var.groupby(year).mean().drop_sel(year=bogus)
                else:
                    var_mean = var.groupby(year).mean()
                self.logger.debug(
                    "year counts: %s",
                    dict(sorted(Counter(year[month_sel].to_numpy()).items())))
                # evaluate weights (i.e. area) for each catchment
                if self.weight is None:
                    weight = 1.0
                else:
                    weight = self.weight.sum((1, 2))
                wvar = weight * var
                wvar_mean = wvar.groupby(year).mean()
                if has_partial_month_sel:
                    wvar_mean = wvar_mean.drop_sel(year=bogus)
                wvar_year_total = wvar_mean.sum(idx_name)
            if has_partial_month_sel:
                # select only the months specified
                var = var.sel(time=month_sel)
            for tstats in time_stats_l:
                if tstats == "mean":
                    values = var.mean("time").values
                elif tstats == "median":
                    if time_window:
                        median_idx = wvar_year_total.argsort()[
                            wvar_year_total.size // 2]
                        median_year = wvar_year_total.year[median_idx].item()
                        self.logger.info("median year is %s", median_year)
                        values = var_mean.sel(year=median_year)
                    else:
                        values = var.median("time").values
                elif tstats == "min":
                    if time_window:
                        min_year = wvar_year_total.year[
                            wvar_year_total.argmin("year")].item()
                        self.logger.info("min year is %s", min_year)
                        values = var_mean.sel(year=min_year)
                    else:
                        values = var.min("time").values
                elif tstats == "max":
                    if time_window:
                        max_year = wvar_year_total.year[
                            wvar_year_total.argmax("year")].item()
                        self.logger.info("max year is %s", max_year)
                        values = var_mean.sel(year=max_year)
                    else:
                        values = var.max("time").values
                elif tstats.startswith("quantile("):
                    qstr = re.findall(r"quantile\(([\d\.eE]+)\)", tstats)
                    if not qstr:
                        raise ValueError(f"error reading {tstats}")
                    q = float(qstr[0])
                    values = var.quantile(q, "time").values
                else:
                    raise ValueError(f"unhandled time stats {tstats!r}")
                with disable_logger(self.logger):
                    ar = self.array_from_values(
                        idx, values, fill=fill, enforce1d=enforce1d)
                ret[tstats] = ar
        else:
            self.logger.debug("time stats are not used for %r", var_name)
            values = var.values
            ar = self.array_from_values(
                idx, values, fill=fill, enforce1d=enforce1d)
            ret[None] = ar

        return ret
