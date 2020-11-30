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


def run_program(program, a, b, c, testN, ref_result, queue):

    program(x1=a, y1=b, z1=c, n=np.int32(testN))
    ref_norm = abs(c[0] - ref_result)

    queue.put(ref_norm)


def run_test(configs, target, implementation, overwrite_y=False):

    testN = int(2**13)

    for config in configs:

        prec = np.float32 if config[2] == dace.float32 else np.float64
        a = aligned_ndarray(np.random.uniform(0, 100, testN).astype(prec),
                            alignment=256)
        b = aligned_ndarray(np.random.uniform(0, 100, testN).astype(prec),
                            alignment=256)

        c = np.zeros(1).astype(prec)

        ref_result = reference_result(a, b)

        program = None
        if target == "fpga":
            program = fpga_graph(config[1],
                                 config[2],
                                 implementation,
                                 testCase=config[3])
        else:
            program = pure_graph(config[2], testCase=config[3])

        ref_norm = 0
        if target == "fpga":

            # Run FPGA tests in a different process to avoid issues with Intel OpenCL tools
            queue = Queue()
            p = Process(target=run_program,
                        args=(program, a, b, c, testN, ref_result,
                              queue))
            p.start()
            p.join()
            ref_norm = queue.get()

        else:
            program(x1=a, y1=b, z1=c, n=np.int32(testN))
            ref_norm = abs(c[0] - ref_result) / ref_result

        passed = ref_norm < 1e-5

        if not passed:
            raise RuntimeError(
                'DOT {} implementation wrong test results on config: '.format(
                    implementation), config)


# ---------- ----------
# Ref result
# ---------- ----------
def reference_result(x_in, y_in):
    return np.dot(x_in, y_in)


# ---------- ----------
# Pure graph program
# ---------- ----------
def pure_graph(precision, implementation="pure", testCase="0"):

    n = dace.symbol("n")

    prec = "single" if precision == dace.float32 else "double"
    test_sdfg = dace.SDFG("dot_test_" + prec + "_" +
                          implementation + "_" + testCase)
    test_state = test_sdfg.add_state("test_state")

    test_sdfg.add_array('x1', shape=[n], dtype=precision)
    test_sdfg.add_array('y1', shape=[n], dtype=precision)
    test_sdfg.add_array('z1', shape=[1], dtype=precision)

    x_in = test_state.add_read('x1')
    y_in = test_state.add_read('y1')
    z_out = test_state.add_write('z1')

    dot_node = blas.dot.Dot("dot", precision)
    dot_node.implementation = implementation

    test_state.add_memlet_path(x_in,
                               dot_node,
                               dst_conn='_x',
                               memlet=Memlet.simple(x_in,
                                                    "0:n"))
    test_state.add_memlet_path(y_in,
                               dot_node,
                               dst_conn='_y',
                               memlet=Memlet.simple(y_in,
                                                    "0:n"))

    test_state.add_memlet_path(dot_node,
                               z_out,
                               src_conn='_result',
                               memlet=Memlet.simple(z_out,
                                                    "0"))

    test_sdfg.expand_library_nodes()

    return test_sdfg.compile()


def test_pure():

    print("Run BLAS test: DOT pure...")

    configs = [(1.0, 1, dace.float32, "0"), (0.0, 1, dace.float32, "1"),
               (random.random(), 1, dace.float32, "2"),
               (1.0, 1, dace.float64, "3")]

    run_test(configs, "pure", "pure")

    print(" --> passed")


# ---------- ----------
# FPGA graph program
# ---------- ----------
def fpga_graph(veclen, precision, vendor, testCase="0"):

    DATATYPE = precision

    n = dace.symbol("n")
    a = dace.symbol("a")

    vendor_mark = "x" if vendor == "xilinx" else "i"
    test_sdfg = dace.SDFG("dot_test_" + vendor_mark + "_" + testCase)
    test_state = test_sdfg.add_state("test_state")

    vecType = dace.vector(precision, veclen)

    test_sdfg.add_symbol(a.name, DATATYPE)

    test_sdfg.add_array('x1', shape=[n / veclen], dtype=vecType)
    test_sdfg.add_array('y1', shape=[n / veclen], dtype=vecType)
    test_sdfg.add_array('z1', shape=[1], dtype=precision)

    dot_node = blas.dot.Dot("dot", DATATYPE, veclen=veclen, partial_width=16, n=n)
    dot_node.implementation = 'fpga_stream'

    x_stream = streaming.StreamReadVector('x1', n, DATATYPE, veclen=veclen)

    y_stream = streaming.StreamReadVector('y1', n, DATATYPE, veclen=veclen)

    z_stream = streaming.StreamWriteVector('z1', 1, DATATYPE)

    preState, postState = streaming.fpga_setup_connect_streamers(
        test_sdfg,
        test_state,
        dot_node, [x_stream, y_stream], ['_x', '_y'],
        dot_node, [z_stream], ['_result'],
        input_memory_banks=[0, 1],
        output_memory_banks=[2])

    test_sdfg.expand_library_nodes()

    mode = "simulation" if vendor == "xilinx" else "emulator"
    dace.config.Config.set("compiler", "fpga_vendor", value=vendor)
    dace.config.Config.set("compiler", vendor, "mode", value=mode)

    return test_sdfg.compile()


def test_fpga(vendor):

    print("Run BLAS test: DOT fpga", vendor + "...")

    configs = [(0.0, 1, dace.float32, "0"), (1.0, 1, dace.float32, "1"),
               (random.random(), 1, dace.float32, "2"),
               (1.0, 1, dace.float64, "3"), (1.0, 4, dace.float64, "4")]

    run_test(configs, "fpga", vendor)

    print(" --> passed")


if __name__ == "__main__":

    cmdParser = argparse.ArgumentParser(allow_abbrev=False)

    cmdParser.add_argument("--target", dest="target", default="pure")

    args = cmdParser.parse_args()

    if args.target == "intel_fpga" or args.target == "xilinx":
        test_fpga(args.target)
    else:
        test_pure()
