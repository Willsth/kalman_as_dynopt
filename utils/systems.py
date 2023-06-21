import numpy as np
from utils.vehicle_simulation import Vehicle, Simulation, plot_car
import utils.cubic_spline_planner as cubic_spline_planner
from scipy.stats import multivariate_normal as mvn
from matplotlib import pyplot as plt
from tqdm.auto import tqdm
from matplotlib import animation
from os.path import exists
import pickle


class CarTrajectoryLinear:
    def __init__(self, Q, R, A, H, N = 100):
        self.Q = Q
        self.R = R
        self.A = A
        self.H = H

        self.N = N
        
        self.dim_m = 4
        self.dim_y = 2
    
    def get_data(self):
        self.x = np.zeros((self.N, self.dim_m))
        self.y = np.zeros((self.N, self.dim_y))
        
        self.x[0] = mvn(mean = np.array([0, 0, 0, 0]), cov = self.Q).rvs(1)
        self.y[0] = mvn(mean = self.H @ self.x[0], cov = self.R).rvs(1)
        
        for i in range(1, self.N):
            self.x[i] = mvn(mean = self.A @ self.x[i - 1], cov = self.Q).rvs(1)
            self.y[i] = mvn(mean = self.H @ self.x[i], cov = self.R).rvs(1)
        
        return self.x, self.y
    
class CarTrajectoryNonLinear:
    def __init__(self, Q, R, f, h, N = 100):
        self.Q = Q
        self.R = R
        self.f = f
        self.h = h

        self.N = N

        self.dim_m = 4
        self.dim_y = 2
    
    def get_data(self):
        self.x = np.zeros((self.N, self.dim_m))
        self.y = np.zeros((self.N, self.dim_y))
        
        self.x[0] = np.array([0, 0, 0, 0])
        self.y[0] = self.h(self.x[0]) + mvn(np.zeros(2), self.R).rvs(1)
        
        for i in range(1, self.N):
            self.x[i] = self.f(self.x[i - 1]) + mvn(np.zeros(4), self.Q).rvs(1)
            self.y[i] = self.h(self.x[i]) + mvn(np.zeros(2), self.R).rvs(1)
        
        return self.x, self.y
    
    
class MPCTrajectory:
    def __init__(self, x_points, y_points, line_segments, savepath=None):

        self.x_points = x_points
        self.y_points = y_points

        self.line_segments = line_segments
        self._states = None
        self._measurements = None

        self.cx = None
        self.cy = None
        self.states_hist = None
        self.controls_hist = None
        
        s1 = 2
        s2 = 2
                
        self.R = np.array([[s1, 0],
                    [0, s2]])
        
        self.savepath = savepath

    @property
    def states(self):
        if self._states is None:
            if self.savepath and exists(self.savepath):
                # Load states, states_hist, controls_hist
                with open(self.savepath, 'rb') as file:
                    tmp = pickle.load(file)
                    
                self._states = tmp['states']
                self.states_hist = tmp['states_hist']
                self.controls_hist = tmp['controls_hist']
                
            else:
                self._states = self._calculate_states()
                if self.savepath:
                    # Pickle save states, states_hist, controls_hist
                    tmp = {'states': self._states, 'states_hist': self.states_hist, 'controls_hist': self.controls_hist}
                    with open(self.savepath, 'wb') as file:
                        pickle.dump(tmp, file)
                    
        return self._states

    @property
    def measurements(self):
        if self._measurements is None:
            self._calculate_measurements()
        return self._measurements
    
    # Based on gut feelings
    def speed_reduction(self, cyaw, sp):
        speed_reduction = np.diff(cyaw)
        speed_reduction = np.concatenate([speed_reduction[int(len(speed_reduction)*0.05):],
                                        np.linspace(speed_reduction[-1], 0, int(len(speed_reduction)*0.05))])
        speed_reduction = np.convolve(speed_reduction, np.ones(len(sp) // 5)/(len(sp) // 5), mode='same')
        speed_reduction = ((1.5*np.max(np.abs(speed_reduction)) - np.abs(speed_reduction)) / (1.5*np.max(np.abs(speed_reduction))))
        speed_reduction = np.clip(speed_reduction, 0, 1)
        speed_reduction *= np.concatenate([np.ones(int(len(speed_reduction)*0.95)+1), np.linspace(1, 0, int(len(speed_reduction)*0.05))])
        
        sp_new = sp
        sp_new[:-1] *= speed_reduction
        
        return sp_new

    def _calculate_states(self):
        self.cx, self.cy, cyaw, ck, s = cubic_spline_planner.calc_spline_course(self.x_points, self.y_points, ds=0.1)
        cyaw = np.abs(cyaw)
        
        initial_state = Vehicle(x=self.cx[0], y=self.cy[0], yaw=cyaw[0], v=0.0)
        dl = 1.0
                
        simulation = Simulation(initial_state, goal_speed=0.5, target_speed=3, max_time = 1000)
        
        # Speed profile
        sp = np.abs(simulation.calc_speed_profile(self.cx, self.cy, cyaw))
        sp = self.speed_reduction(cyaw, sp)
        
        # Simulation
        self.states_hist, self.controls_hist = simulation.simulate(self.cx, self.cy, cyaw, ck, dl, sp=sp)

        # Get simulation states
        x, y, v, yaw = self.states_hist['x'], self.states_hist['y'], self.states_hist['v'], self.states_hist['yaw']        

        # Calculate dx and dy
        dx = np.empty_like(x)
        dy = np.empty_like(y)
        for i in range(len(x)):
            dx[i] = v[i] * np.cos(yaw[i]) * initial_state.dt
            dy[i] = v[i] * np.sin(yaw[i]) * initial_state.dt

        return np.c_[x, y, dx, dy]

    def h(self, x):
        H = np.array([[1, 0, 0, 0],
                        [0, 1, 0, 0]])
        
        if x.ndim == 1:
            x = x.reshape(1, -1)     
            out = np.dot(x, H.T) + np.array([1.0 * np.sin(x[:, 1]), -1.0 * np.cos(x[:, 0])]).T
            out = out.flatten()
            
        else:
            out = np.dot(x, H.T) + np.array([1.0 * np.sin(x[:, 1]), -1.0 * np.cos(x[:, 0])]).T
        
        return out
    
    def _calculate_measurements(self):
        measurements = self.h(self._states) + mvn([0, 0], self.R).rvs(len(self._states))        
        self._measurements = measurements

    def get_data(self):
        return self.states.copy(), self.measurements.copy()
    
    def get_bounding_box(self):
        min_x, max_x = np.inf, -np.inf
        min_y, max_y = np.inf, -np.inf
        
        for line_segment in self.line_segments:
            min_x = np.min([min_x, np.min(line_segment[:, 0])])
            max_x = np.max([max_x, np.max(line_segment[:, 0])])
            min_y = np.min([min_y, np.min(line_segment[:, 1])])
            max_y = np.max([max_y, np.max(line_segment[:, 1])])

        min_x = np.min([min_x, np.min(self.x_points), np.min(self.states[:, 0]), np.min(self.measurements[:, 0])])
        max_x = np.max([max_x, np.max(self.x_points), np.max(self.states[:, 0]), np.max(self.measurements[:, 0])])
        min_y = np.min([min_y, np.min(self.y_points), np.min(self.states[:, 1]), np.min(self.measurements[:, 1])])
        max_y = np.max([max_y, np.max(self.y_points), np.max(self.states[:, 1]), np.max(self.measurements[:, 1])])
        
        return min_x, max_x, min_y, max_y
    
    def plot(self):
        fig, ax = plt.subplots(1, 1, figsize=(24, 6))

        ax.plot(self.measurements[:, 0], self.measurements[:, 1], 'o', label='Measurements', markersize=3)
        ax.plot(self.x_points, self.y_points, 'x', label='Waypoints', markersize=10)
        ax.plot(self.states[:, 0], self.states[:, 1], label='Trajectory', linewidth=2)
        
        for line_segment in self.line_segments:
            ax.plot(line_segment[:, 0], line_segment[:, 1], 'k-', label='Boundaries', linewidth=1)
            
        min_x, max_x, min_y, max_y = self.get_bounding_box()      

        ax.set_xlim(min_x - 3, max_x + 3)
        ax.set_ylim(min_y - 3, max_y + 3)
        ax.set_aspect('equal')

        ax.legend()

        plt.show()

    def animate(self, filename='animation'):
        fig, ax = plt.subplots(1, 1, figsize=(16, 6))
        
        target_inds = self.controls_hist['target_inds'] 
        t = self.states_hist['t']
        v = self.states_hist['v']
        yaw = self.states_hist['yaw']
        d = self.controls_hist['d']
                
        min_x, max_x, min_y, max_y = self.get_bounding_box()      

        def aux_animate(i):
            ax.cla()
            ax.plot(self.x_points, self.y_points, "kx", markersize=10)
            ax.plot(self.states[:i, 0], self.states[:i, 1], "-r", label="trajectory")
            ax.plot(self.measurements[:i, 0], self.measurements[:i, 1], 'bx', markersize=3, label="measurements")
            ax.plot(self.cx[target_inds[i]], self.cy[target_inds[i]], "xg", label="target")

            plot_car(ax, self.states[i, 0], self.states[i, 1], yaw[i], steer=d[i])

            for line_segment in self.line_segments:
                ax.plot(line_segment[:, 0], line_segment[:, 1], 'k-', label='Boundaries', linewidth=1)
            
            ax.set_title("Time [s]:" + str(round(t[i], 2)) + ", speed [km/h]:" + str(round(v[i] * 3.6, 2)))
            
            ax.grid(True)
            ax.set_xlim(min_x - 3, max_x + 3)
            ax.set_ylim(min_y - 3, max_y + 3)
            ax.set_aspect('equal')

        ani = animation.FuncAnimation(fig, aux_animate, frames=len(t), repeat=False)

        # Save animation with progress bar
        with tqdm(total=len(t)) as pbar:
            ani.save(f'{filename}.gif', writer='Pillow', fps=25, progress_callback=lambda i, n: pbar.update())
            
        plt.close()


def track_example1(seed=None):
    
    if seed:
        np.random.seed(seed)

    line_segments = [np.array([(1,1), (1,5), (40,5), (40,20), (45,20), (45,1), (1,1)])]

    x1 = np.linspace(5, 43, 6)
    x2 = np.repeat(42.5, 3) + np.random.normal(0, 0.75, 3)
    x2 = np.clip(x2, 41, 44)

    y1 = np.repeat(3, 6) + np.random.normal(0, 0.75, 6)
    y1 = np.clip(y1, 2, 4)

    y2 = np.linspace(7.5, 17.5, 3)

    x_coords = np.r_[2, x1, x2, 42.5]
    y_coords = np.r_[3, y1, y2, 19]

    return x_coords, y_coords, line_segments