import random

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
import imageio
import io
from PIL import Image
from tqdm import tqdm
from Mesh.HigherOrderMesh.decode_triangle_indices import decode_triangle_indices
from Mesh.HigherOrderMesh.generate_ijk_indices import generate_ijk_indices
from Simulator.HigherOrderElements.shape_functions import silvester_shape_function

def make_sim_result_gif_1(FEM_V, FEM_encodings, result, num_nodes_x, num_nodes_y, traction, time, time_step_size, file_name, element_order):
    n = num_nodes_x * num_nodes_y

    images = []

    num_time_steps_per_frame = 30
    time_rate = 1

    sample_points = generate_ijk_indices(40) / 40

    for i in tqdm(range(0, len(result.nodal_displacements), num_time_steps_per_frame), desc='Creating GIF'):
        # make a Figure and attach it to a canvas.
        fig = Figure()
        canvas = FigureCanvasAgg(fig)

        # Do some plotting here
        ax = fig.add_subplot(111)

        deformed_V = FEM_V + result.nodal_displacements[i].reshape((n, 2))

        reference_points = []
        interpolated_points = []
        for i, encoding in enumerate(FEM_encodings):
            global_indices, ijk_indices = decode_triangle_indices(encoding, element_order)
            for sample_point in sample_points:
                N_vals = []
                for i, ijk_index in enumerate(ijk_indices):
                    N_val = silvester_shape_function(ijk_index, sample_point, element_order)
                    N_vals.append(N_val)
                N_vals = np.array(N_vals)

                interpolated_point = N_vals @ deformed_V[global_indices]
                interpolated_points.append(interpolated_point)
                interpolated_reference_point = N_vals @ FEM_V[global_indices]
                reference_points.append(interpolated_reference_point)

        interpolated_points = np.array(interpolated_points)
        reference_points = np.array(reference_points)

        reference = ax.scatter(reference_points[:, 0], reference_points[:, 1])
        deformed = ax.scatter(interpolated_points[:, 0], interpolated_points[:, 1])

        # ax.legend(handles=[reference, deformed], loc='upper left')
        ax.set_xlabel(r'$X_1$')
        ax.set_xlim(-3.1, 3.5)
        ax.set_ylim(-4.6, 2)
        ax.set_ylabel(r'$X_2$')
        ax.set_title('Showing the deformed cantilever mesh compared with the reference mesh.')

        # Trick to write PNG into memory buffer and read it using PIL
        with io.BytesIO() as out:
            fig.savefig(out, format="png")  # Add dpi= to match your figsize
            pic = Image.open(out)
            pix = np.array(pic.getdata(), dtype=np.uint8).reshape(pic.size[1], pic.size[0], -1)
        images.append(pix)


        # # Retrieve a view on the renderer buffer
        # canvas.draw()
        # buf = canvas.buffer_rgba()
        # # convert to a NumPy array
        # plot_image = np.asarray(buf)
        # images.append(plot_image)

    # kargs = {'duration': time_step_size*num_time_steps_per_frame}
    imageio.mimsave(file_name + '.gif', images, duration=time_step_size*num_time_steps_per_frame*time_rate)
