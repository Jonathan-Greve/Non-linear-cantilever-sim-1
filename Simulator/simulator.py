# Simulator class
# Containts the main loop of the simulator called simulate
import numpy as np
import quadpy
from scipy.spatial import Delaunay
from tqdm import tqdm

from Mesh.Cantilever.area_computations import compute_triangle_element_area, \
    compute_all_element_areas
from Mesh.Cantilever.generate_2d_cantilever_delaunay import generate_2d_cantilever_delaunay
from Mesh.Cantilever.generate_2d_cantilever_kennys import generate_2d_cantilever_kennys
from Mesh.HigherOrderMesh.decode_triangle_indices import decode_triangle_indices
from Mesh.HigherOrderMesh.generate_FEM_mesh import generate_FEM_mesh
from Simulator.HigherOrderElements.shape_functions import silvester_shape_function, \
    shape_function_spatial_derivative, vandermonde_shape_function, vandermonde_spatial_derivative
from Simulator.cartesian_to_barycentric import cartesian_to_barycentric
from Simulator.integral_computations import compute_shape_function_volume
from Simulator.result import Result
from Simulator.triangle_shape_functions import triangle_shape_function_i_helper, \
    triangle_shape_function_j_helper, triangle_shape_function_k_helper


class Simulator:
    def __init__(self, number_of_time_steps, time_step, material_properties,
                 length, height, number_of_nodes_x, number_of_nodes_y, traction_force, gravity,
                 element_order=1):
        # Simulation settings
        self.number_of_time_steps = number_of_time_steps
        self.time_step = time_step
        self.gravity = gravity

        # Material settings
        self.material_properties = material_properties
        # self.lambda_ = (self.material_properties.youngs_modulus * self.material_properties.poisson_ratio
        #         / ((1 + self.material_properties.poisson_ratio) *
        #            (1 - 2 * self.material_properties.poisson_ratio)))
        # self.mu = (self.material_properties.youngs_modulus /
        #       (2 * (1 + self.material_properties.poisson_ratio)))

        self.lambda_ = (
                (self.material_properties.youngs_modulus * self.material_properties.poisson_ratio) /
                ((1+self.material_properties.poisson_ratio)*(1-2*self.material_properties.poisson_ratio))
        )
        self.mu = (
                self.material_properties.youngs_modulus /
                (2 * (1+self.material_properties.poisson_ratio))
        )

        # Cantilever settings
        self.length = length
        self.height = height
        self.number_of_nodes_x = number_of_nodes_x
        self.number_of_nodes_y = number_of_nodes_y
        self.traction_force = traction_force

        # Element settings
        self.element_order = element_order

        # Initialize the cantilever mesh
        points, faces = generate_2d_cantilever_delaunay(self.length, self.height,
                                                     self.number_of_nodes_x, self.number_of_nodes_y)
        # points, faces = generate_2d_cantilever_kennys(self.length, self.height,
        #                                                 self.number_of_nodes_x, self.number_of_nodes_y)
        self.mesh_points = points.astype(np.float64)
        self.mesh_faces = faces
        self.all_A_e = compute_all_element_areas(self.mesh_points, self.mesh_faces)

        # All volume under shape functions
        self.all_V_e = np.array([compute_shape_function_volume(self.mesh_points, face) for face in self.mesh_faces], dtype=np.float64)


        # FEM mesh vertices, ijk_index for every V in FEM_V, global indice encoding for every V in FEM_V
        self.FEM_V, self.FEM_encoding = generate_FEM_mesh(self.mesh_points, self.mesh_faces, self.element_order)
        self.total_number_of_nodes = len(self.FEM_V)

        # Boundary node indices
        self.boundary_len = 0.0001
        self.dirichlet_boundary_indices_x = []
        for i, vertex in enumerate(self.FEM_V):
            if vertex[0] < 0 - (self.length / 2) + self.boundary_len:
                self.dirichlet_boundary_indices_x.append(2*i)

        self.dirichlet_boundary_indices_x = np.array(self.dirichlet_boundary_indices_x, dtype=np.int32)
        self.dirichlet_boundary_indices_y = self.dirichlet_boundary_indices_x + 1

        def check_if_traction_node(vertex):
            if vertex[0] > 0 + (self.length / 2) - self.boundary_len:
                return True
            else:
                return False

        # list of (encoding_index, edge_index). Edge index: 0 for ij, 1 for jk, 2 for ki
        self.traction_encodings = []
        for i, encoding in enumerate(self.FEM_encoding):
            global_indices, ijk_indices = decode_triangle_indices(encoding, self.element_order)

            is_i_traction_node = check_if_traction_node(self.FEM_V[global_indices[0]])
            is_j_traction_node = check_if_traction_node(self.FEM_V[global_indices[1]])
            is_k_traction_node = check_if_traction_node(self.FEM_V[global_indices[2]])

            # Check ij-edge
            if is_i_traction_node and is_j_traction_node:
                self.traction_encodings.append((i, 0))

            # Check jk-edge
            if is_j_traction_node and is_k_traction_node:
                self.traction_encodings.append((i, 1))
            # Check ki-edge
            if is_k_traction_node and is_i_traction_node:
                self.traction_encodings.append((i, 2))

        print("Simulator initialized")

    def simulate(self):
        # Initialize variables
        time = 0.0

        print("Simulation started...")
        print("----------------------------------------------------")
        print("Simulation settings:")
        print("  Time to simulate: {}".format(self.time_step * self.number_of_time_steps))
        print("  Time step: {}".format(self.time_step))
        print("  Number of time steps: {}".format(self.number_of_time_steps))
        print("----------------------------------------------------")

        # Precompute some variables
        M = self.compute_mass_matrix()
        Minv = np.linalg.inv(M)
        C = self.compute_damping_matrix()
        f_t = self.compute_traction_forces()
        f_g = self.compute_body_forces(include_gravity=True)
        f = - f_t - f_g

        # Add boundary conditions
        # f[self.dirichlet_boundary_indices_x] = 0
        # f[self.dirichlet_boundary_indices_y] = 0

        u_n = np.zeros(self.total_number_of_nodes * 2, dtype=np.float64)
        v_n = np.zeros(self.total_number_of_nodes * 2, dtype=np.float64)
        a_n = np.zeros(self.total_number_of_nodes * 2, dtype=np.float64)
        # a_n[np.arange(1, self.total_number_of_nodes * 2, 2)] = self.gravity[1]
        x_n = self.FEM_V.reshape([self.total_number_of_nodes * 2])

        times = [0]
        displacements = [u_n]
        velocities = [v_n]
        accelerations = [a_n]
        Es = [np.zeros([len(self.mesh_faces), 2,2], dtype=np.float64)]
        Ms = [M]
        damping_forces = [C@v_n]

        # Main loop
        for i in tqdm(range(self.number_of_time_steps), desc="Running simulation"):
            time_step_size = np.min([self.time_step, self.number_of_time_steps*self.time_step - time])
            # Compute stiffness matrix
            k, E = self.compute_stiffness_matrix(x_n)

            # Do simulation step
            damping_term = np.dot(C, v_n)

            # # Remove all forces after 1 sec.
            # if (i * self.time_step > 1):
            #     f = f * 0

            forces = f - damping_term - k
            # forces[np.abs(forces) < 1e-10] = 0

            a_n_1 = np.dot(Minv,  forces)

            # M_condition_num = np.linalg.cond(M)
            # Minv_condition_num = np.linalg.cond(Minv)
            # C_condition_num = np.linalg.cond(C)

            # Dirichlet boundary conditions (set acceleration to 0 on the fixed boundary nodes)
            # a_n_1[self.dirichlet_boundary_indices_x] = 0
            # a_n_1[self.dirichlet_boundary_indices_y] = 0

            v_n_1 = v_n + self.time_step * a_n_1
            v_n_1[self.dirichlet_boundary_indices_x] = 0
            v_n_1[self.dirichlet_boundary_indices_y] = 0
            v_n_1[np.abs(v_n_1) < 1e-10] = 0

            x_n_1 = x_n + self.time_step * v_n_1

            # New displacements
            u_n = x_n_1 - self.FEM_V.reshape([self.total_number_of_nodes * 2])

            # New velocities
            v_n = v_n_1

            # New positions
            x_n = x_n_1


            # Update time
            time += self.time_step
            times.append(time)
            displacements.append(u_n)
            velocities.append(v_n)
            accelerations.append(a_n_1)
            Es.append(E)
            Ms.append(M)
            damping_forces.append(damping_term)


            # Print time
            # print(f"i: {i}. Time: {time}")

        return Result(times, np.array(displacements), velocities, accelerations, Es, Ms, damping_forces, f_g)

    def compute_integral_N_squared(self, triangle_encoding):
        # Compute matrix using quadpy (quadpy is a quadrature package)
        global_indices, ijk_indices = decode_triangle_indices(triangle_encoding, self.element_order)
        V_e = self.FEM_V[global_indices]

        # Number of nodes
        m = (self.element_order + 1) * (self.element_order + 2) // 2
        if len(global_indices) != m:
            raise Exception("Number of nodes in element is not correct")

        i,j,k = global_indices[0:3]

        # Corner vertices of triangle
        triangle = self.mesh_points[[i,j,k]]

        scheme = quadpy.t2.get_good_scheme(self.element_order*2+1)

        # N is 2xm so the "square" matrix given by the outer product with itself is 2m x 2m
        integral_N_square = np.zeros((m*2, m*2))
        for i in range(2 * m):
            for j in range(2 * m):
                if i % 2 == 0 and j % 2 != 0:
                    continue
                if i % 2 != 0 and j % 2 == 0:
                    continue
                if i == j:
                    def f(x):
                        xi = cartesian_to_barycentric(x, triangle)
                        shape_function = silvester_shape_function(ijk_indices[int(i / 2)], xi, self.element_order)
                        return shape_function ** 2
                else:
                    def f(x):
                        xi = cartesian_to_barycentric(x, triangle)
                        shape_function_1 = silvester_shape_function(ijk_indices[int(i / 2)], xi, self.element_order)
                        shape_function_2 = silvester_shape_function(ijk_indices[int(j / 2)], xi, self.element_order)

                        return shape_function_1 * shape_function_2

                integral_N_square[i,j] = scheme.integrate(f, triangle)

        return integral_N_square

    def compute_mass_matrix(self):
        def compute_element_mass_matrix(face_index):
            triangle_encoding = self.FEM_encoding[face_index]
            integral_N_square = self.compute_integral_N_squared(triangle_encoding)
            return integral_N_square * self.material_properties.density

        # Compute all element mass matrices
        all_M_e = np.array([compute_element_mass_matrix(i) for i in range(len(self.mesh_faces))], dtype=np.float64)

        # Assemble the mass matrix
        M = self.assemble_square_matrix(all_M_e)

        return M


    def compute_damping_matrix(self):
        def compute_element_damping_matrix(face_index):
            triangle_encoding = self.FEM_encoding[face_index]
            integral_N_square = self.compute_integral_N_squared(triangle_encoding)
            return integral_N_square * self.material_properties.density

        # Compute all element mass matrices
        all_C_e = np.array([compute_element_damping_matrix(i) for i in range(len(self.mesh_faces))], dtype=np.float64)

        # Assemble the mass matrix
        C = self.assemble_square_matrix(all_C_e)


        return C * self.material_properties.damping_coefficient

    def compute_stiffness_matrix(self, x_n):
        m = int((self.element_order + 1) * (self.element_order + 2) / 2)
        def compute_element_stiffness_matrix(face_index):
            triangle_encoding = self.FEM_encoding[face_index]
            global_indices, ijk_indices = decode_triangle_indices(triangle_encoding, self.element_order)
            triangle = self.FEM_V[global_indices[0:3]]

            # Reference vertices (non-deformed vertices)
            V_e = self.FEM_V[global_indices]
            v_e = []
            for i in range(m):
                x = x_n[global_indices[i]*2]
                y = x_n[global_indices[i]*2+1]
                v_e.append(np.array([x,y]))

            u_e = v_e - V_e

            # Compute F for each shape function (i.e. for each node of the element)
            F = np.eye(2)
            for i in range(m):
                # dN = shape_function_spatial_derivative(V_e, triangle_encoding, V_e[i],
                #                                        self.element_order)
                dN_vandermonde = vandermonde_spatial_derivative(V_e, V_e[i], self.element_order).T

                # x_i = v_e[i][0]
                # y_i = v_e[i][1]
                #
                # F_i = np.array([
                #     [dN[i,0] * x_i, dN[i,1] * x_i],
                #     [dN[i,0] * y_i, dN[i,1] * y_i]
                # ])
                F+= np.outer(dN_vandermonde[i],u_e[i])


            i = global_indices[0]
            j = global_indices[1]
            k = global_indices[2]

            x_i = np.array([x_n[i*2], x_n[i*2+1]])
            x_j = np.array([x_n[j*2], x_n[j*2+1]])
            x_k = np.array([x_n[k*2], x_n[k*2+1]])

            # F = self.compute_F(x_i, x_j, x_k, triangle[0], triangle[1], triangle[2])

            # print(f'Triangle {triangle_face}')
            # F = self.compute_F(
            #     np.array([x_i, y_i]),
            #     np.array([x_j, y_j]),
            #     np.array([x_k, y_k]),
            #     np.array([X_i, Y_i]),
            #     np.array([X_j, Y_j]),
            #     np.array([X_k, Y_k]),
            # )

            # Compute E
            I = np.eye(2, dtype=np.float64)
            C = np.dot(np.transpose(F), F)
            E = (C - I) / 2.0

            # Compute S
            try:
                S = self.lambda_ * np.trace(E) * I + 2 * self.mu * E
            except:
                print('1')

            scheme = quadpy.t2.get_good_scheme(self.element_order)
            quad_points = scheme.points
            quad_weights = scheme.weights

            A_e = self.all_A_e[face_index]

            k_e = np.zeros(2*m)
            for i in range(m):
                def f(x):
                    dN = shape_function_spatial_derivative(V_e, triangle_encoding, x,
                                                       self.element_order)
                    P = F @ S
                    result = (P @ dN[i])

                    return result

                val = 0
                for j in range(len(quad_points[0])):
                    point_bary = quad_points[:,j]
                    point = triangle[0] * point_bary[0] + \
                            triangle[1] * point_bary[1] + \
                            triangle[2] * point_bary[2]
                    f_val = f(point)
                    val += f_val * quad_weights[j]

                val *= A_e

                # val_x = scheme.integrate(f_x, triangle)
                # val_y = scheme.integrate(f_y, triangle)

                k_e[2*i] = val[0]
                k_e[2*i+1] = val[1]

            return k_e, E, F

        # Computes all element stiffness matrices
        all_k_e_matrices = np.zeros([len(self.mesh_faces), 2*m], dtype=np.float64)
        all_Es = np.zeros([len(self.mesh_faces), 2, 2])
        all_Fs = np.zeros([len(self.mesh_faces), 2, 2])
        for i in range(len(self.mesh_faces)):
            triangle_face = self.mesh_faces[i]
            A_e = self.all_A_e[i]
            k_e, E, F = compute_element_stiffness_matrix(i)
            all_k_e_matrices[i] = k_e
            all_Es[i] = E
            all_Fs[i] = F

        # Assemble the stiffness matrix
        k = np.zeros([2 * self.total_number_of_nodes], dtype=np.float64)
        for i in range(len(self.mesh_faces)):
            triangle_encoding = self.FEM_encoding[i]

            global_indices, ijk_indices = decode_triangle_indices(triangle_encoding,
                                                                  self.element_order)
            global_indices_list = []
            for j in range(len(global_indices)):
                global_indices_list.append(global_indices[j] * 2)
                global_indices_list.append(global_indices[j] * 2 + 1)
            global_indices_list = np.array(global_indices_list)

            k[global_indices_list] += all_k_e_matrices[i]


        # print("F: {}".format(all_Fs[0]))
        # print("CE: {}".format(all_Es[0]))
        # print("F: {}".format(all_Fs[1]))
        # print("CE: {}".format(all_Es[1]))
        # print('-------------------------------------')
        return k, all_Es

    def assemble_square_matrix(self, all_M_e):
        matrix = np.zeros([2 * self.total_number_of_nodes, 2 * self.total_number_of_nodes], dtype=np.float64)
        assert(len(self.mesh_faces) == len(self.FEM_encoding))
        for i in range(len(self.FEM_encoding)):
            triangle_encoding = self.FEM_encoding[i]

            global_indices, ijk_indices = decode_triangle_indices(triangle_encoding, self.element_order)

            global_indices_list = []
            for j in range(len(global_indices)):
                global_indices_list.append(global_indices[j] * 2)
                global_indices_list.append(global_indices[j] * 2 + 1)
            global_indices_list = np.array(global_indices_list)

            matrix[global_indices_list.reshape([2*len(global_indices), 1]), global_indices_list] += all_M_e[i]

        return matrix

    def compute_body_forces(self, include_gravity=True):
        f_b = np.zeros([2 * self.total_number_of_nodes])

        # Number of nodes in the element
        m = int((self.element_order + 1) * (self.element_order + 2) / 2)

        # Add gravity force to all nodes
        if include_gravity:
            scheme = quadpy.t2.get_good_scheme(self.element_order + 1)

            def compute_element_gravity_term(face_index):
                triangle = self.mesh_points[self.mesh_faces[face_index]]

                triangle_encoding = self.FEM_encoding[face_index]
                global_indices, ijk_indices = decode_triangle_indices(triangle_encoding, self.element_order)

                N_int_values = np.zeros([2, 2*m])
                for i in range(len(global_indices)):
                    def f(x):
                        xi = cartesian_to_barycentric(x, triangle)
                        shape_function_val = silvester_shape_function(ijk_indices[i], xi,
                                                                  self.element_order)
                        # shape_function_val_vander = vandermonde_shape_function(self.FEM_V[global_indices], x, self.element_order)[:,i]
                        return shape_function_val

                    int_val = scheme.integrate(f, triangle)
                    N_int_values[0, i*2] = int_val
                    N_int_values[1, 1 + i*2] = int_val

                f_g_e = self.material_properties.density * N_int_values.T@self.gravity

                return f_g_e

            all_gravity_terms = np.zeros([len(self.mesh_faces), 2*m], dtype=np.float64)
            for i in range(len(self.mesh_faces)):
                f_g_e = compute_element_gravity_term(i)
                all_gravity_terms[i] = f_g_e

            # Assemble the stiffness matrix
            f_g = np.zeros([2 * self.total_number_of_nodes], dtype=np.float64)
            for i in range(len(self.mesh_faces)):
                triangle_encoding = self.FEM_encoding[i]

                global_indices, ijk_indices = decode_triangle_indices(triangle_encoding,
                                                                      self.element_order)
                global_indices_list = []
                for j in range(len(global_indices)):
                    global_indices_list.append(global_indices[j] * 2)
                    global_indices_list.append(global_indices[j] * 2 + 1)
                global_indices_list = np.array(global_indices_list)

                f_g[global_indices_list] += all_gravity_terms[i]

            P_0g = - f_g

            f_b += P_0g

        return f_b

    def compute_traction_forces(self):
        """
            Compute the traction vector acting on the object. It takes as input a
            traction vector with dimension: 2x1. Then returns the (2n)x1 vector
            with all the nodes on the traction edge set to the traction of this parameter.

            :param traction: The traction vector applied to all the nodes on the traction edge.

            :return: A (2n)x1 vector.
            """

        num_internal_nodes = (self.element_order -1) * (self.element_order - 2) / 2

        def compute_element_traction(traction_encoding_index):
            traction_encoding = self.traction_encodings[traction_encoding_index]
            triangle_encoding = self.FEM_encoding[traction_encoding[0]]

            global_indices, ijk_indices = decode_triangle_indices(triangle_encoding, self.element_order)
            num_edge_nodes = self.element_order - 1

            i,j,k = global_indices[0:3]

            # If traction edge is ij-edge
            local_edge_indices = []
            if traction_encoding[1] == 0:
                local_edge_indices.append(0)
                edge_start_index = 3 + num_internal_nodes
                local_edge_indices.extend(np.arange(edge_start_index, edge_start_index + num_edge_nodes))
                local_edge_indices.append(1)
            # Elif traction edge is jk-edge
            elif traction_encoding[1] == 1:
                local_edge_indices.append(1)
                edge_start_index = 3 + num_internal_nodes + num_edge_nodes
                local_edge_indices.extend(np.arange(edge_start_index, edge_start_index + num_edge_nodes))
                local_edge_indices.append(2)
            # Elif traction edge is ki-edge
            elif traction_encoding[1] == 2:
                local_edge_indices.append(2)
                edge_start_index = 3 + num_internal_nodes + num_edge_nodes * 2
                local_edge_indices.extend(np.arange(edge_start_index, edge_start_index + num_edge_nodes))
                local_edge_indices.append(0)

            local_edge_indices = np.array(local_edge_indices, dtype=np.int32)

            edge_indices = global_indices[local_edge_indices]
            # Assumes edge is fully vertical
            element_length = np.abs(self.FEM_V[edge_indices[0]][1] - self.FEM_V[edge_indices[-1]][1])

            scheme = quadpy.c1.gauss_patterson(self.element_order + 1)

            N_int_vals = np.zeros([2, len(local_edge_indices)*2])
            for l in range(len(local_edge_indices)):
                def f(x):
                    # Compute barycentric coordinates
                    xi = None
                    if traction_encoding[1] == 0:
                        xi_3 = x * 0
                        xi_1 = 1 - x/element_length
                        xi_2 = 1 - xi_1
                        xi = np.array([xi_1, xi_2, xi_3])
                    elif traction_encoding[1] == 1:
                        xi_1 = x * 0
                        xi_2 = 1 - x/element_length
                        xi_3 = 1 - xi_2
                        xi = np.array([xi_1, xi_2, xi_3])
                    elif traction_encoding[1] == 2:
                        xi_2 = x * 0
                        xi_3 = 1 - x/element_length
                        xi_1 = 1 - xi_3
                        xi = np.array([xi_1, xi_2, xi_3])

                    shape_function_val = silvester_shape_function(
                        ijk_indices[local_edge_indices[l]], xi, self.element_order)

                    return shape_function_val

                val = scheme.integrate(f, [0.0, element_length])

                N_int_vals[0, l*2] = val
                N_int_vals[1, 1 + l*2] = val

            f_t_e = N_int_vals.T@self.traction_force

            f_t_e_global_indices = global_indices[local_edge_indices]

            return f_t_e, f_t_e_global_indices

        m = int((self.element_order + 1) * (self.element_order + 2) / 2)
        all_traction_terms = []
        all_traction_indices = []
        for i in range(len(self.traction_encodings)):
            f_t_e, f_t_e_indices = compute_element_traction(i)
            all_traction_terms.append(f_t_e)
            all_traction_indices.append(f_t_e_indices)

        f_t = np.zeros([2 * self.total_number_of_nodes], dtype=np.float64)
        for i in range(len(all_traction_terms)):
            f_t_e = all_traction_terms[i]
            global_indices = all_traction_indices[i]

            global_indices_list = []
            for j in range(len(global_indices)):
                global_indices_list.append(global_indices[j] * 2)
                global_indices_list.append(global_indices[j] * 2 + 1)
            global_indices_list = np.array(global_indices_list)

            f_t[global_indices_list] += f_t_e

        return -f_t

    def compute_F(self, x_i, x_j, x_k, X_i, X_j, X_k):
        x_ij = (x_j - x_i)
        x_ik = (x_k - x_i)

        X_ij = (X_j - X_i)
        X_ik = (X_k - X_i)

        D = np.array([
            [x_ij[0], x_ik[0]],
            [x_ij[1], x_ik[1]],
        ], dtype=np.float64)

        D_0 = np.array([
            [X_ij[0], X_ik[0]],
            [X_ij[1], X_ik[1]],
        ], dtype=np.float64)

        F = D @ np.linalg.inv(D_0)
        return F

