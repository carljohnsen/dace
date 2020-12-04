#!/usr/bin/env python3

import numpy as np

import argparse
import scipy
import random

import dace
from dace.memlet import Memlet

import dace.libraries.blas as blas
import dace.libraries.blas.utility.fpga_helper as streaming
from dace.libraries.blas.utility import memory_operations as memOps
from dace.transformation.interstate import GPUTransformSDFG

from dace.libraries.standard.memory import aligned_ndarray

from multiprocessing import Process, Queue



# ---------- ----------
# FPGA graph program
# ---------- ----------
def fpga_graph(veclen, precision, vendor, testCase="0"):

    DATATYPE = precision
    nRows = dace.symbol("n")
    mCols = dace.symbol("m")

    a = dace.symbol("alpha")
    b = dace.symbol("beta")

    # TODO: expand tests to consider different tile size configs
    rowTile = 4
    colTile = 4
    partialWidth = 2
    vecM = veclen

    vendor_mark = "x" if vendor == "xilinx" else "i"
    test_sdfg = dace.SDFG("gemv_test_" + vendor_mark + "_" + testCase)
    test_state = test_sdfg.add_state("test_state")

    test_sdfg.add_symbol(a.name, DATATYPE)

    if b != 0:
        test_sdfg.add_symbol(b.name, DATATYPE)

    test_sdfg.add_array('A', shape=[nRows*mCols], dtype=DATATYPE)
    test_sdfg.add_array('x', shape=[mCols], dtype=DATATYPE)
    test_sdfg.add_array('y', shape=[nRows], dtype=DATATYPE)
    #test_sdfg.add_array('y', shape=[nRows], dtype=DATATYPE)

    x_stream = streaming.StreamReadVector(
        'x',
        mCols,
        DATATYPE,
        veclen=vecM,
        repeat='{}/{}'.format(nRows, rowTile)
    )

    y_stream = None
    if b != 0:
        y_stream = streaming.StreamReadVector(
            'y',
            nRows,
            DATATYPE,
            veclen=1,
        )

    A_stream = streaming.StreamReadMatrixFull(
        'A',
        nRows,
        mCols,
        rowTile,
        colTile,
        DATATYPE,
        tileByRow=True,
        veclen=vecM
    )

    res_stream = streaming.StreamWriteVector(
        'y',
        nRows,
        DATATYPE
    )

    gemv_node = blas.gemv.Gemv(
        "blas_gemv",
        dtype=DATATYPE,
        n_tile=rowTile,
        m_tile=colTile,
        partial_width=partialWidth,
        n=nRows,
        m=mCols,
        veclen=vecM,
        alpha=a, beta=b
    )
    gemv_node.implementation = 'fpga_stream'

    preState, postState = streaming.fpga_setup_connect_streamers(
        test_sdfg,
        test_state,
        gemv_node, [x_stream, y_stream, A_stream], ['_x', '_y', '_A'],
        gemv_node, [res_stream], ['_y']
    )

    test_sdfg.expand_library_nodes()

    mode = "simulation" if vendor == "xilinx" else "emulator"
    dace.config.Config.set("compiler", "fpga_vendor", value=vendor)
    dace.config.Config.set("compiler", vendor, "mode", value=mode)

    return test_sdfg




# ---------- ----------
# Pure graph program (CPU)
# ---------- ----------
def pure_graph(dtype, transposed):
    n = dace.symbol("n")
    m = dace.symbol("m")

    sdfg = dace.SDFG("gemv")

    # alpha and beta are symbols
    sdfg.add_symbol("alpha", dtype)
    sdfg.add_symbol("beta", dtype)

    state = sdfg.add_state("gemv_compute")

    A_rows = n
    A_cols = m
    x_size = n if transposed else m
    y_size = m if transposed else n

    sdfg.add_array('A', shape=[A_rows, A_cols], dtype=dtype)
    sdfg.add_array('x', shape=[x_size], dtype=dtype)
    sdfg.add_array('y', shape=[y_size], dtype=dtype)

    A = state.add_read("A")
    x = state.add_read("x")
    result = state.add_write("y")

    gemv_node = blas.Gemv("gemv",
                          dtype=dace.float32,
                          transA=transposed)

    state.add_memlet_path(A,
                          gemv_node,
                          dst_conn="_A",
                          memlet=Memlet.simple(
                              A, "0:{}, 0:{}".format(A_rows, A_cols)))
    state.add_memlet_path(x,
                          gemv_node,
                          dst_conn="_x",
                          memlet=Memlet.simple(x,
                                               "0:{}".format(x_size)))
    y = state.add_read("y")
    state.add_memlet_path(y,
                          gemv_node,
                          dst_conn="_y",
                          memlet=Memlet.simple(y,
                                               "0:{}".format(y_size)))
    state.add_memlet_path(gemv_node,
                          result,
                          src_conn="_y",
                          memlet=Memlet.simple(result, "0:{}".format(y_size)))
    return sdfg


# ---------- ----------
# Intel FPGA graph
# ---------- ----------
def intel_fpga_graph(dtype, transposed, vec_width=4):
    n = dace.symbol("n")
    m = dace.symbol("m")



    if transposed:
        tile_m_size = dace.symbol("tile_m_size")
    sdfg = dace.SDFG("gemv")
    sdfg.add_symbol("tile_m_size", int)
    # alpha and beta are symbols
    sdfg.add_symbol("alpha", dtype)
    sdfg.add_symbol("beta", dtype)

    A_rows = n
    A_cols = m
    x_size = n if transposed else m
    y_size = m if transposed else n

    ###########################################################################
    # Copy data to FPGA

    copy_in_state = sdfg.add_state("copy_to_device")

    sdfg.add_array("A", shape=[n, m], dtype=dtype)
    sdfg.add_array("x", shape=[x_size], dtype=dtype)
    sdfg.add_array("y", shape=[y_size], dtype=dtype)

    in_host_A = copy_in_state.add_read("A")
    in_host_x = copy_in_state.add_read("x")
    in_host_y = copy_in_state.add_read("y")

    sdfg.add_array("device_A", shape=[A_rows, A_cols], dtype=dtype, storage=dace.dtypes.StorageType.FPGA_Global,
                   transient=True)
    sdfg.add_array("device_x", shape=[x_size], dtype=dtype, storage=dace.dtypes.StorageType.FPGA_Global,
                   transient=True)
    sdfg.add_array("device_y", shape=[y_size], dtype=dtype, storage=dace.dtypes.StorageType.FPGA_Global,
                   transient=True)

    in_device_A = copy_in_state.add_write("device_A")
    in_device_x = copy_in_state.add_write("device_x")
    in_device_y = copy_in_state.add_write("device_y")

    copy_in_state.add_memlet_path(
        in_host_A, in_device_A,
        memlet=Memlet.simple(in_host_A, "0:{}, 0:{}".format(A_rows, A_cols))
    )
    copy_in_state.add_memlet_path(
        in_host_x, in_device_x,
        memlet=Memlet.simple(in_host_x, "0:{}".format(x_size))
    )
    copy_in_state.add_memlet_path(
        in_host_y, in_device_y,
        memlet=Memlet.simple(in_host_y, "0:{}".format(y_size))
    )

    ###########################################################################
    # Copy data from FPGA

    copy_out_state = sdfg.add_state("copy_to_host")

    out_device = copy_out_state.add_read("device_y")
    out_host = copy_out_state.add_write("y")

    copy_out_state.add_memlet_path(
        out_device, out_host,
        memlet=Memlet.simple(out_host, "0:{}".format(y_size))
    )

    ########################################################################
    # FPGA State

    fpga_state = sdfg.add_state("gemv_computation")
    # This should not be an FPGA kernel, rather the gemv_expanded nested SDFG should

    A = fpga_state.add_read("device_A")
    x = fpga_state.add_read("device_x")
    y_in = fpga_state.add_read("device_y")
    y_out = fpga_state.add_write("device_y")


    gemv_node = blas.Gemv("gemv", dtype=dace.float32, vec_width=vec_width, transA=transposed)
    gemv_node.implementation = "IntelFPGA"

    fpga_state.add_memlet_path(A,
                               gemv_node,
                               dst_conn="_A",
                               memlet=Memlet.simple(A, "0:{}, 0:{}".format(n, m)))

    fpga_state.add_memlet_path(x,
                               gemv_node,
                               dst_conn="_x",
                               memlet=Memlet.simple(x, "0:{}".format("{}".format(x_size))))
    fpga_state.add_memlet_path(y_in,
                               gemv_node,
                               dst_conn="_y",
                               memlet=Memlet.simple(y_in, "0:{}".format(y_size)))
    fpga_state.add_memlet_path(gemv_node,
                               y_out,
                               src_conn="_y",
                               memlet=Memlet.simple(y_out, "0:{}".format(y_size)))

    ######################################
    # Interstate edges
    sdfg.add_edge(copy_in_state, fpga_state,
                  dace.sdfg.sdfg.InterstateEdge())
    sdfg.add_edge(fpga_state, copy_out_state,
                  dace.sdfg.sdfg.InterstateEdge())

    sdfg.fill_scope_connectors()
    return sdfg


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("N", type=int, nargs="?", default=2)
    parser.add_argument("M", type=int, nargs="?", default=2)
    parser.add_argument("alpha", type=int, nargs="?", default=1)
    parser.add_argument("beta", type=int, nargs="?", default=0)
    parser.add_argument("--transposed",
                        action="store_true",
                        default=False,
                        help="Compute GEMV with transposed matrix")
    parser.add_argument("--target", dest="target", default="pure")

    args = parser.parse_args()
    n = args.N
    m = args.M
    alpha = args.alpha
    beta = args.beta
    transposed = args.transposed
    if args.target == "pure":
        sdfg = pure_graph(dace.float32, transposed)
    elif args.target == "xilinx":
        sdfg = fpga_graph(1, dace.float32, args.target, "0")
    elif args.target == "intel_fpga":
        sdfg = intel_fpga_graph(dace.float32, transposed)
    else:
        print("Unsupported target")
        exit(-1)

    sdfg.save('aoeu.sdfg')

    A = np.random.rand(n, m).astype(np.float32)
    x = np.random.rand(n if transposed else m).astype(np.float32)
    y = np.random.rand(m if transposed else n).astype(np.float32)

    y_copy = np.copy(y)

    sdfg(A=A, x=x, y=y, n=n, m=m, alpha=alpha, beta=beta)

    ref = scipy.linalg.blas.sgemv(alpha, A, x, beta, y_copy, trans=transposed)

    diff = np.linalg.norm(y - ref) / (m if transposed else n)
    if diff >= 1e-5:
        print("Error")
    else:
        print("Ok")
