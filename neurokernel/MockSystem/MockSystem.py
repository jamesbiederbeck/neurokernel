import sys
import numpy as np
import scipy as sp
import random as rd
import numpy.random as np_rd
import atexit
import pycuda.gpuarray as garray
import pycuda.driver as cuda
from pycuda.compiler import SourceModule
from pycuda.tools import dtype_to_ctype
from neurokernel.tools import parray
from neurokernel.Module import Module

class MockSystem(Module):
    """
    Neural network class. This code, by now, is provided by the user. In this
    example, this code is the lamina version implemented by Nikul and Yiyin.
    """
    def __init__(self, manager, num_neurons_per_type, avr_synapses_per_neuron,
                 dt, num_in_non, num_in_spike, num_proj_non, num_proj_spike,
                 device):

        np.random.seed(0)

        Module.__init__(self, manager, dt, num_in_non, num_in_spike,
                        num_proj_non, num_proj_spike, device)

        self.num_neurons = num_neurons_per_type * 15
        self.num_synapses = int(avr_synapses_per_neuron * self.num_neurons)

        # It corresponds to different neuron types and these types mean
        # a different set of parameters. In this example, there is 15 types of
        # neurons.  
        start_idx = np.asarray([self.num_neurons / 15 * i for i in range(15)],
                               dtype = np.int32)
        self.start_idx = start_idx
        self.num_types = start_idx.size

    def init_gpu(self):

        # In order to understand pre_neuron, post_neuron and dendrites it's
        # necessary notice that the process is over the synapses instead of
        # neurons. So, in fact there is no neurons, but connection between
        # neurons. Number of dendrites per neuron. A dendrite is a neuron's
        pre_neuron = np_rd.random_integers(0, self.num_neurons,
                                size = (self.num_synapses,)).astype(np.int32)

        post_neuron = np.sort(np_rd.random_integers(0, self.num_neurons,
                                size = (self.num_synapses,)).astype(np.int32))

        self.num_dendrites = sp.bincount(post_neuron)

        # Parameters of the model: threshold, slope, saturation, Vs and phy.
        # Shape: (num_synapses,)
        thres = np.asarray([rd.gauss(-.5, .01) for x in \
                            np.zeros([self.num_synapses])], dtype = np.float64)
        slope = np.asarray([rd.gauss(-.5, .1) for x in \
                            np.zeros([self.num_synapses])], dtype = np.float64)
        saturation = np.asarray([rd.gauss(.1, .01) for x in \
                            np.zeros([self.num_synapses])], dtype = np.float64)
        power = np.ones([self.num_synapses], dtype = np.float64)
        reverse = np.asarray([rd.gauss(-.4, .1) for x in \
                            np.zeros([self.num_synapses])], dtype = np.float64)
        V_1 = np.asarray([rd.gauss(.13, .03) for x in \
                          np.zeros([self.num_types])], dtype = np.float64)
        V_2 = np.asarray([rd.gauss(.15, .001) for x in \
                          np.zeros([self.num_types])], dtype = np.float64)
        V_3 = np.asarray([rd.gauss(-.25, .1) for x in \
                          np.zeros([self.num_types])], dtype = np.float64)
        V_4 = np.asarray([rd.gauss(.15, .05) for x in \
                          np.zeros([self.num_types])], dtype = np.float64)
        Tphi = np.asarray([rd.gauss(.2, .01) for x in \
                           np.zeros([self.num_types])], dtype = np.float64)
        offset = np.array([ 0. , 0. , 0. , 0. , 0. , 0. , 0.2, 0.2, 0.2, 0.2,
                           0.2, 0.2, 0.2, 0.2, 0. ], dtype = np.float64)

        # Parameters of alpha function. Shape: (num_synapses,)
        delay = np.ones([self.num_synapses], dtype = np.float64)

        # Initial condition at resting potential. Shape of both: (num_neurons,)
        V = np.asarray([rd.gauss(-.51, .01) for x in \
                        np.zeros([self.num_neurons])], dtype = np.float64)
        n = np.asarray([rd.gauss(.3, .05) for x in \
                        np.zeros([self.num_neurons])], dtype = np.float64)

        self.delay_steps = int(round(max(delay) * 1e-3 / self.dt))

        self.buffer = CircularArray(self.num_neurons, self.delay_steps, V)

        self.neurons = MorrisLecar(self.num_neurons, self.num_types,
                                   self.num_neurons / 15, self.start_idx,
                                   self.dt, self.num_dendrites, V, n, V_1, V_2,
                                   V_3, V_4, Tphi, offset,
                                   self.num_in_non / self.num_neurons)
        self.synapses = VectorSynapse(self.num_synapses, pre_neuron, post_neuron,
                                      thres, slope, power, saturation, delay,
                                      reverse, self.dt)

    def run_step(self, in_non_list = None, in_spike_list = None,
                 proj_non = None, proj_spike = None):

        self.neurons.I_pre.fill(0)
        self.neurons.update_I_pre_input(in_non_list)

        self.neurons.read_synapse(self.synapses.conductance,
                                  self.synapses.V_rev)

        self.neurons.eval(self.buffer)

        self.synapses.compute_synapse(self.buffer)

        cuda.memcpy_dtoh(proj_non, self.neurons.V.gpudata)
        self.buffer.step()

class CircularArray:
    def __init__(self, num_neurons, delay_steps, rest):
        self.dtype = np.double
        self.num_neurons = num_neurons
        self.delay_steps = delay_steps

        self.buffer = parray.empty((delay_steps, num_neurons), np.double)

        d_rest = garray.to_gpu(rest)
        self.current = 0

        #initializing V buffer
        for i in range(delay_steps):
            cuda.memcpy_dtod(int(self.buffer.gpudata) + self.buffer.ld * i,
                             d_rest.gpudata, d_rest.nbytes)

    def step(self):
        self.current += 1
        if self.current >= self.delay_steps:
            self.current = 0

class MorrisLecar:
    def __init__(self, num_neurons, num_types, num_cart, neuron_start, dt,
                 num_dendrite, V, n, V1, V2, V3, V4, Tphi, offset,
                 non_input_start):
        """
        Set Morris Lecar neurons in the network.

        Parameters
        ----------
        N : int
            Number of neurons to be added.
        """
        self.dtype = np.double
        self.num_cart = num_cart
        self.num_types = num_types
        self.num_neurons = num_neurons
        self.dt = dt
        self.steps = max(int(round(dt / 1e-5)), 1)

        self.ddt = dt / self.steps

        self.V = garray.to_gpu(V)
        self.n = garray.to_gpu(n)

        self.I_pre = garray.zeros(self.num_neurons, np.double)

        self.h_V = cuda.pagelocked_empty((self.num_types, self.num_cart),
                                         np.double)

        self.cum_num_dendrite = garray.to_gpu(np.concatenate((np.asarray([0, ],
                                                dtype = np.int32),
                                                np.cumsum(num_dendrite,
                                                          dtype = np.int32))))
        self.num_dendrite = garray.to_gpu(num_dendrite)
        self.num_input = int(neuron_start[non_input_start])

        self.update = self.get_euler_kernel(neuron_start, V1, V2, V3, V4,
                                            Tphi, offset)
        self.get_input = self.get_input_func()

    def update_I_pre_input(self, I_ext):
        cuda.memcpy_dtod(int(self.I_pre.gpudata), I_ext, self.num_input * self.I_pre.dtype.itemsize)

    def read_synapse(self, conductance, V_rev, st = None):
        self.get_input.prepared_async_call(self.grid_get_input,
                                           self.block_get_input, st,
                                           conductance.gpudata,
                                           self.cum_num_dendrite.gpudata,
                                           self.num_dendrite.gpudata,
                                           self.I_pre.gpudata, self.V.gpudata,
                                           V_rev.gpudata)

    def eval(self, buffer, st = None):
        self.update.prepared_async_call(self.update_grid, self.update_block,
                                        st, self.V.gpudata, self.n.gpudata,
                                        int(buffer.buffer.gpudata) + \
                                        buffer.current * buffer.buffer.ld * \
                                        buffer.buffer.dtype.itemsize,
                                        self.num_neurons, self.I_pre.gpudata,
                                        self.ddt * 1000, self.steps)

    def get_euler_kernel(self, neuron_start, V1, V2, V3, V4, Tphi, offset):
        template = open('neurokernel/MockSystem/cuda_code/euler_kernel.cu', 'r')

        dtype = self.dtype
        scalartype = dtype.type if dtype.__class__ is np.dtype else dtype
        self.update_block = (128, 1, 1)
        self.update_grid = ((self.num_neurons - 1) / 128 + 1, 1)
        mod = SourceModule(template.read() % {"type": dtype_to_ctype(dtype),
                                              "ntype": self.num_types,
                                              "nneu": self.update_block[0]},
                           options = ["--ptxas-options=-v"])
        func = mod.get_function("hhn_euler_multiple")

        V1_addr, V1_nbytes = mod.get_global("V_1")
        V2_addr, V2_nbytes = mod.get_global("V_2")
        V3_addr, V3_nbytes = mod.get_global("V_3")
        V4_addr, V4_nbytes = mod.get_global("V_4")
        Tphi_addr, Tphi_nbytes = mod.get_global("Tphi")
        neuron_start_addr, neuron_start_nbytes = mod.get_global("neuron_start")
        offset_addr, offset_nbytes = mod.get_global("offset")

        cuda.memcpy_htod(V1_addr, V1)
        cuda.memcpy_htod(V2_addr, V2)
        cuda.memcpy_htod(V3_addr, V3)
        cuda.memcpy_htod(V4_addr, V4)
        cuda.memcpy_htod(Tphi_addr, Tphi)
        cuda.memcpy_htod(neuron_start_addr, neuron_start)
        cuda.memcpy_htod(offset_addr, offset)

        func.prepare([np.intp, np.intp, np.intp, np.int32, np.intp, scalartype,
                      np.int32])

        return func

    def get_euler_kernel1(self, neuron_start, V1, V2, V3, V4, Tphi, offset):
        template = open('neurokernel/MockSystem/cuda_code/euler_kernel1.cu', 'r')

        dtype = self.dtype
        scalartype = dtype.type if dtype.__class__ is np.dtype else dtype
        mod = SourceModule(template.read() % {"type": dtype_to_ctype(dtype),
                                              "ntype": self.num_types},
                           options = ["--ptxas-options=-v"])
        func = mod.get_function("hhn_euler_multiple")

        V1_addr, V1_nbytes = mod.get_global("V_1")
        V2_addr, V2_nbytes = mod.get_global("V_2")
        V3_addr, V3_nbytes = mod.get_global("V_3")
        V4_addr, V4_nbytes = mod.get_global("V_4")
        Tphi_addr, Tphi_nbytes = mod.get_global("Tphi")
        neuron_start_addr, neuron_start_nbytes = mod.get_global("neuron_start")
        offset_addr, offset_nbytes = mod.get_global("offset")


        cuda.memcpy_htod(V1_addr, V1)
        cuda.memcpy_htod(V2_addr, V2)
        cuda.memcpy_htod(V3_addr, V3)
        cuda.memcpy_htod(V4_addr, V4)
        cuda.memcpy_htod(Tphi_addr, Tphi)
        cuda.memcpy_htod(neuron_start_addr, neuron_start)
        cuda.memcpy_htod(offset_addr, offset)

        func.prepare([np.intp, np.intp, np.intp, np.int32, np.intp, scalartype,
                      np.int32])

        self.update_block = (64, 2, 1)
        self.update_grid = ((self.num_neurons - 1) / 64 + 1, 1)

        return func

    def get_input_func(self):
        template = open('neurokernel/MockSystem/cuda_code/input_func.cu', 'r')

        mod = SourceModule(template.read() % {"num_neurons": self.num_neurons},
                           options = ["--ptxas-options=-v"])
        func = mod.get_function("get_input")
        func.prepare([np.intp, np.intp, np.intp, np.intp, np.intp, np.intp])
        self.block_get_input = (32, 32, 1)
        self.grid_get_input = ((self.num_neurons - 1) / 32 + 1, 1)

        return func

class VectorSynapse:
    def __init__(self, num_synapse, pre_neuron, post_neuron, syn_thres,
                 syn_slope, syn_power, syn_saturation, syn_delay, V_rev, dt):

        self.dt = dt
        self.num_synapse = num_synapse
        self.pre_neuron = garray.to_gpu(pre_neuron)
        #self.post_neuron = garray.to_gpu(post_neuron)

        self.threshold = garray.to_gpu(syn_thres)
        self.slope = garray.to_gpu(syn_slope)
        self.power = garray.to_gpu(syn_power)
        self.saturation = garray.to_gpu(syn_saturation)
        self.delay = garray.to_gpu(
                            np.round(syn_delay * 1e-3 / dt).astype(np.int32))
        self.conductance = garray.zeros(self.num_synapse, np.double)

        self.V_rev = garray.to_gpu(V_rev)

        self.update_terminal_synapse = self.get_update_terminal_synapse_func()
        self.mem_tmp = garray.empty(self.num_synapse, np.double)

    def compute_synapse(self, buffer, st = None):
        self.update_terminal_synapse.prepared_async_call(
                                                    self.grid_terminal_synapse,
                                                    self.block_terminal_synapse,
                                                    st, buffer.buffer.gpudata,
                                                    buffer.buffer.ld,
                                                    buffer.current,
                                                    buffer.delay_steps,
                                                    self.pre_neuron.gpudata,
                                                    self.conductance.gpudata,
                                                    self.threshold.gpudata,
                                                    self.slope.gpudata,
                                                    self.power.gpudata,
                                                    self.saturation.gpudata,
                                                    self.delay.gpudata,
                                                    self.mem_tmp.gpudata)

    def get_update_terminal_synapse_func(self):
        template = open('neurokernel/MockSystem/cuda_code/terminal_synapse.cu', 'r')

        mod = SourceModule(template.read() % {"n_synapse": self.num_synapse},
                           options = ["--ptxas-options=-v"])
        func = mod.get_function("update_terminal_synapse")
        func.prepare([np.intp, np.int32, np.int32, np.int32, np.intp, np.intp,
                      np.intp, np.intp, np.intp, np.intp, np.intp, np.intp])
        self.block_terminal_synapse = (256, 1, 1)
        self.grid_terminal_synapse = (min(6 * \
                              cuda.Context.get_device().MULTIPROCESSOR_COUNT,
                              (self.num_synapse - 1) / 256 + 1), 1)

        return func

def main(argv):

    manager = None
    try:
        num_neurons = int(sys.argv[1][:-1])
        avr_synapses = np.double(sys.argv[2][:-1])
        dt = np.double(sys.argv[3][:-1])
        num_in_non = int(sys.argv[4][:-1])
        num_in_spike = int(sys.argv[5][:-1])
        num_proj_non = int(sys.argv[6][:-1])
        num_proj_spike = int(sys.argv[7][:-1])
        device = int(sys.argv[8])
    except IOError:
        print "Wrong number of parameters. Exemple: 768, 6, 1e-4, " + \
              "4608, 0, 4608, 0, 1"

    cuda.init()
    ctx = cuda.Device(device).make_context()
    atexit.register(ctx.pop)

    start = cuda.Event()
    end = cuda.Event()

    system = MockSystem(manager, num_neurons, avr_synapses, dt, num_in_non,
                 num_in_spike, num_proj_non, num_proj_spike, device)

    system.init_gpu()

    I_ext = parray.to_gpu(np.ones([1 / system.dt, system.num_in_non]))
    out = np.empty((1 / system.dt, num_proj_non), np.double)

    start.record()
    for i in range(int(1 / system.dt)):
        system.run_step(int(I_ext.gpudata) + I_ext.dtype.itemsize * \
                        I_ext.ld * i, None, out[i, :], None)

    end.record()
    end.synchronize()
    secs = start.time_till(end) * 1e-3
    print "Time: %fs" % secs

if __name__ == '__main__':

    # number of neurons per type that will be multiplied by 15
    # average number of synapses per neuron 
    # parameters = 768, 6, 1e-4, 4608, 0, 4608, 0, 1
    main(sys.argv[1:])
