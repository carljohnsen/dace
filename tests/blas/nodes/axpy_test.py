import dace
from dace.memlet import Memlet
import dace.libraries.blas as blas
import numpy as np
import scipy
from tqdm import tqdm
import argparse

from dace.transformation.interstate import GPUTransformSDFG


# ---------- ----------
# Arguments
# ---------- ----------
cmdParser = argparse.ArgumentParser(allow_abbrev=False)

cmdParser.add_argument("--cublas", dest="cublas", action='store_true')
cmdParser.add_argument("--mkl", dest="mkl", action='store_true')
cmdParser.add_argument("--openblas", dest="openblas", action='store_true')
cmdParser.add_argument("--pure", dest="pure", action='store_true')
cmdParser.add_argument("--xilinx", dest="xilinx", action='store_true')
cmdParser.add_argument("--intel_fpga", dest="intel_fpga", action='store_true')

args = cmdParser.parse_args()


# ---------- ----------
# Ref result
# ---------- ----------
def reference_result(x_in, y_in, alpha):
    return scipy.linalg.blas.saxpy(x_in, y_in, a=alpha)


# ---------- ----------
# Pure graph program
# ---------- ----------
def pure_graph(vecWidth, precision, implementation="pure"):
    
    n = dace.symbol("n")
    a = dace.symbol("a")

    test_sdfg = dace.SDFG("saxpy_test")
    test_state = test_sdfg.add_state("test_state")

    test_sdfg.add_symbol(a.name, precision)

    test_sdfg.add_array('x1', shape=[n], dtype=precision)
    test_sdfg.add_array('y1', shape=[n], dtype=precision)
    test_sdfg.add_array('z1', shape=[n], dtype=precision)

    x_in = test_state.add_read('x1')
    y_in = test_state.add_read('y1')
    z_out = test_state.add_write('z1')

    saxpy_node = blas.axpy.Axpy("axpy", precision, vecWidth=vecWidth)
    saxpy_node.implementation = implementation

    test_state.add_memlet_path(
        x_in, saxpy_node,
        dst_conn='_x',
        memlet=Memlet.simple(x_in, "0:n", num_accesses=n, veclen=vecWidth)
    )
    test_state.add_memlet_path(
        y_in, saxpy_node,
        dst_conn='_y',
        memlet=Memlet.simple(y_in, "0:n", num_accesses=n, veclen=vecWidth)
    )

    test_state.add_memlet_path(
        saxpy_node, z_out,
        src_conn='_res',
        memlet=Memlet.simple(z_out, "0:n", num_accesses=n, veclen=vecWidth)
    )

    if saxpy_node.implementation == "cublas":  
        test_sdfg.apply_transformations(GPUTransformSDFG)

    test_sdfg.expand_library_nodes()

    return test_sdfg.compile(optimizer=False)


def test_pure():

    print("Run BLAS test: AXPY pure", end="")

    configs = [
        (1.0, 1, dace.float32),
        (0.0, 1, dace.float32),
        (random.random(), 1, dace.float32),
        (1.0, 1, dace.float64),
        (1.0, 4, dace.float64)
    ]

    testN = int(2**13)

    for config in configs:

        prec = np.float32 if config[2] == dace.float32 else np.float64
        a = np.random.randint(100, size=testN).astype(prec)
        b = np.random.randint(100, size=testN).astype(prec)

        c = np.zeros(testSize).astype(prec)
        alpha = np.float32(config[0]) if config[2] == dace.float32 else np.float64(config[0])

        ref_result = reference_result(a, b, alpha)

        compiledGraph = pure_graph(config[1], config[2])

        compiledGraph(x1=a, y1=b, a=alpha, z1=c, n=np.int32(testN))

        ref_norm = np.linalg.norm(c - ref_result) / testN
        passed = ref_norm < 1e-5

        if not passed:
            raise RuntimeError('AXPY pure implementation wrong test results')

    print(" --> passed")


# ---------- ----------
# CPU library graph program
# ---------- ----------
def cpu_graph(precision, implementation):
    return pure_graph(1, precision, implementation=implementation)


def test_cpu(implementation):
    
    print("Run BLAS test: AXPY", implementation, end="")

    configs = [
        (1.0, 1, dace.float32),
        (0.0, 1, dace.float32),
        (random.random(), 1, dace.float32),
        (1.0, 1, dace.float64)
    ]

    testN = int(2**13)

    for config in configs:

        prec = np.float32 if config[2] == dace.float32 else np.float64
        a = np.random.randint(100, size=testN).astype(prec)
        b = np.random.randint(100, size=testN).astype(prec)

        # c = np.zeros(testSize).astype(prec)
        alpha = np.float32(config[0]) if config[2] == dace.float32 else np.float64(config[0])

        ref_result = reference_result(a, b, alpha)

        compiledGraph = cpu_graph(config[2], implementation)

        compiledGraph(x1=a, y1=b, a=alpha, z1=b, n=np.int32(testN))

        ref_norm = np.linalg.norm(c - ref_result) / testN
        passed = ref_norm < 1e-5

        if not passed:
            raise RuntimeError("AXPY " + implementation + " implementation wrong test results')

    print(" --> passed")


# ---------- ----------
# GPU Cuda graph program
# ---------- ----------
# def gpu_graph():
#     return pure_graph(1, precision, implementation=implementation)


def test_gpu():
    test_cpu("cublas")



# ---------- ----------
# FPGA graph program
# ---------- ----------
def fpga_graph(vecWidth, precision, vendor):


    print("Run BLAS test: AXPY fpga", end="")
    
    DATATYPE = precision

    n = dace.symbol("n")
    a = dace.symbol("a")

    test_sdfg = dace.SDFG("saxpy_perf_stream_double")
    test_state = test_sdfg.add_state("test_state")

    test_sdfg.add_symbol(a.name, DATATYPE)

    test_sdfg.add_array('x1', shape=[n], dtype=DATATYPE)
    test_sdfg.add_array('y1', shape=[n], dtype=DATATYPE)
    test_sdfg.add_array('z1', shape=[n], dtype=DATATYPE)

    saxpy_node = blas.level1.axpy.Axpy("saxpy", DATATYPE , vecWidth=vecWidth, n=n, a=a)
    saxpy_node.implementation = 'fpga_stream'

    x_stream = streaming.streamReadVector(
        'x1',
        n,
        typeDace,
        vecWidth=vecWidth
    )

    y_stream = streaming.streamReadVector(
        'y1',
        n,
        typeDace,
        vecWidth=vecWidth
    )

    z_stream = streaming.streamWriteVector(
        'z1',
        n,
        typeDace,
        vecWidth=vecWidth
    )

    preState, postState = streaming.fpga_setupConnectStreamers(
        test_sdfg,
        test_state,
        saxpy_node,
        [x_stream, y_stream],
        ['_x', '_y'],
        saxpy_node,
        [z_stream],
        ['_res'],
        inputMemoryBanks=[0, 1],
        outputMemoryBanks=[2]
    )



    test_sdfg.expand_library_nodes()


def test_fpga(vendor):
    pass



if __name__ == "__main__":
    
    if args.pure:
        test_pure()

    if args.mkl:
        test_cpu("mkl")

    if args.openblas:
        test_cpu("openblas")

    if args.cublas:
        test_gpu()

    if args.xilinx:
        test_fpga("xilinx")

    if args.intel_fpga:
        test_fpga("intel_fpga")