

from fitparse import FitFile
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
import matplotlib.colors as colors
from typing import Literal
import datetime

from IPython.display import display,Markdown,HTML
import folium

import os
import numpy as np
import json
import requests


class CyclingData:
    
    drag_coeffictient = 1.0
    g = 9.807
    rho0 = 1.225 # air volumic mass at 0m
    L = 0.0065 # dT°/dz
    T0=288.15 # temperature constant
    R = 287.05 # gas constant
    
    mass = 70
    size = 1.80
    bike_mass = 10
    
    
    data:pd.DataFrame
    """
    Dataframe containing the data of the activity, with the additional computations.
    
    Columns:
        time: Timestamp (date and time)
        activity_time : time since start (seconds)
        position: Distance traveled (m)
        altitude: Altitude (m)
        speed: Speed (m/s)
        heart_rate: Heart rate (bpm)
        drag: Drag (N)
        kinetic_energy: Kinetic energy (J)
        potential_energy: Potential energy (J)
        time_delta: time elapsed since last mesure (s)
        position_delta: distance traveled since last mesure (m)
        slope: slope*100 is slope in %
        watts: (J)
        
    """
    
    def __init__(self,filename:str=None,reload_altitude:bool=False)->None:
        """
        Args:
            filename (str): name of the file to be read in activities folder (so complete path to the file is "fit_file/<filename>)
            reload_altitude (bool): sometimes the elevation from the Garmin isn't very accurate, this can be used to replace the altitude data from Garmin by geographical data (from France). Defaults to False.
        """
        if filename==None:
            return
        
        assert filename.endswith('.fit'),"Please provide a .fit file (select activity on https://connect.garmin.com/modern/activities?activityType=cycling and export to original format)"
        self._fitfile = FitFile("activites/"+filename)
        
        ####################################
        # TRANSFORM FITFILE INTO DATAFRAME #
        ####################################
        
        # create df
        _data = []
        for mesure in self._fitfile.get_messages('record'):
            mesure_dict = {}
            for mesure_column in mesure:
                mesure_dict[mesure_column.name] = mesure_column.value
            _data.append(mesure_dict)
        self.data = pd.DataFrame(_data)
        
        if not "heart_rate" in self.data.columns:
            self.data["heart_rate"] = 180
        
        
        if "altitude" in self.data.columns:
            if not "enhanced_altitude" in self.data.columns:
                self.data["enhanced_altitude"] = self.data["altitude"]
            self.data.drop(columns=["altitude"],inplace=True)
            
        if "speed" in self.data.columns:
            if not "enhanced_speed" in self.data.columns:
                self.data["enhanced_speed"] = self.data["speed"]
            self.data.drop(columns=["speed"],inplace=True)
        
        self.data.rename(
            columns={
                "enhanced_altitude":"altitude",
                "enhanced_speed":"speed",
                "position_lat":"lat",
                "position_long":"lon",
                "timestamp":"time",
                "distance":"position"
            },
            inplace=True
        )
        
        self.data = self.data[["time","position","altitude","speed","heart_rate","lon","lat"]]
        
        self.data["lon"] = self.data["lon"]/11930465 # type conversion from binary to degrees
        self.data["lat"] = self.data["lat"]/11930465
        
        # recompute altitude
        if reload_altitude:
            try:
                self._overwrite_altitude_with_ign()
            except:
                print("Error while retrieving altitude data")
        
        # analyse absolute values
        self.data["time"] = pd.to_datetime(self.data["time"])
        self.data["drag"] = CyclingData.compute_drag(
            self.mass,
            self.size,
            self.data["speed"],
            self.data["altitude"]            
        )
        self.data["kinetic_energy"] = 0.5 * (self.mass+self.bike_mass) * self.data["speed"]**2
        self.data["potential_energy"] = (self.mass+self.bike_mass) * CyclingData.g * self.data["altitude"]
        
        
        
        # analyse relative values
        absolute_columns = ["time","position","altitude","speed","kinetic_energy","potential_energy"]
        for col in absolute_columns:
            self.data[f"{col}_delta"] = self.data[col].diff()
        self.data = self.data.iloc[1:] # first row of delta is all nan
        self.data["time_delta"] = self.data["time_delta"].dt.total_seconds()
        
        # remove pauses in the ride
        self.data = self.data[
            (self.data["position_delta"]>0.1) &\
                ((self.data["time_delta"]<10) | (self.data["position_delta"]>self.data["time_delta"]*1))
        ] # if movement < 10 cm we consider that the bike is not moving
        # if no mesure over 10s and movement is slower than 1m/s (3.6 km/h), bike is not moving
        
        self.data["activity_time"] = self.data["time_delta"].cumsum()
        
        #################################
        # COMPUTE ADDITIONAL QUANTITIES #
        #################################
        
        self.data["slope"] = self.data["altitude_delta"] / self.data["position_delta"]
        # 100*slope is the slope in %
        #self.data["slope"] = self.data["slope"].rolling(window=5,min_periods=1).median()
        # in order to remove anomalies, we add a rolling median to our date
        # TODO: added rolling to other variables, like heart rate or altitude earlier
        
        energy_delta = self.data["potential_energy_delta"]+self.data["kinetic_energy_delta"] # delta_energy / dt
        applied_power = energy_delta / self.data["time_delta"]
        drag_power = self.data["drag"]*self.data["speed"]
        
        self.data["watts"] = applied_power + drag_power # (watts-drag)*dt = delta_energy
        self.data.loc[self.data["watts"]<0,"watts"] = 0 # remove braking
    
    def get_data(self)->pd.DataFrame:
        """
        Dataframe containing the data of the activity, with the additional computations.
        
        Columns:
            time: Timestamp (date and time)
            activity_time : time since start (seconds)
            position: Distance traveled (m)
            altitude: Altitude (m)
            speed: Speed (m/s)
            heart_rate: Heart rate (bpm)
            drag: Drag (N)
            kinetic_energy: Kinetic energy (J)
            potential_energy: Potential energy (J)
            time_delta: time elapsed since last mesure (s)
            position_delta: distance traveled since last mesure (m)
            slope: slope*100 is slope in %
            watts: (J)
            
        """
        return self.data.copy()

    def min_periods(self,minutes:int=0,seconds:int=0)->int:
        """
        Returns:
            int: 95% confidence upper bound of time delta. Used to define min_periods with rolling
        """
        tau = self.data["time_delta"].mean() + self.data["time_delta"].std()*2
        return int((60*minutes + seconds) / tau)
        
        
    ###################   
    # PLOTS (GENERAL) #
    ###################
    
    def set_x_axis(self,ax:plt.Axes,axis_type:Literal["index","mesure","time","activity_time","position","distance"])->pd.Series:
        """
        Returns:
            pd.Series: X to use aftewards with ax.plot(X,...) or ax.bar(X,...)
        """
        if axis_type=="distance":
            axis_type="position"
        if axis_type=="time":
            axis_type="activity_time"
        if axis_type=="mesure":
            axis_type="index"
        
        
        if axis_type=="index":
            X=self.get_data().index
        else:
            X=self.get_data()[axis_type]
        
        if axis_type=="position":
            X/=1000 # get km
            
        
        ax.set_xlim(X.min(),X.max())
        
        labels = {
            "index":"Mesure",
            "activity_time":"Temps",
            "position":"Distance (km)"
        }
        
        ax.set_xlabel(labels[axis_type])
        
        if axis_type=="activity_time":
            ax.set_xticks(
                np.linspace(0,X.max(),10),
                labels=[f"{str(datetime.timedelta(seconds=int(x)))}" for x in np.linspace(0,X.max(),10)]
            )
                    
        return X.copy()
    
    def set_y_axis(self,ax:plt.Axes,axis_type:Literal["time_delta","altitude","speed","heart_rate","watts","slope"])->pd.Series:
        """
        Returns:
            pd.Series: Y to use aftewards with ax.plot(.,Y,...) or ax.bar(.,Y,...)
        """
        Y = self.get_data()[axis_type]
        
        if axis_type=="slope":
            Y*=100 # get %
        
        if axis_type=="speed":
            Y*=3.6 # get km/h
        
        y_max = Y.max()
        y_min = Y.min()
        
        y_min,y_max = y_min - (y_max-y_min)/10 , y_max + (y_max-y_min)/10
        
        if Y.min()>=0:
            y_min = max(0,y_min)
            
        labels = {
            "time_delta":"Intervalle (s)",
            "altitude":"Altitude (m)",
            "speed":"Vitesse (km/h)",
            "heart_rate":"Fréquence Cardiaque (bpm)",
            "watts":"Puissance (W)",
            "slope":"Pente (%)"
        }
        
        ax.set_ylabel(labels[axis_type])
        ax.set_ylim(y_min,y_max)
        
        return Y.copy()
    
    
    def show_mesure_delta(self):
        fig,ax = plt.subplots(figsize=(20,4))
        
        ax.set_title("Temps entre deux mesures consécutives")
        X = self.set_x_axis(ax,"index")
        Y = self.set_y_axis(ax,"time_delta")
        
        ax.bar(X,Y,width=1) 
        plt.show()
    
    
    #################
    # PLOTS (TRACK) #
    #################
    
    def show_global_informations(self):
        display(Markdown(f"""
<center>

| Durée de l'activité | Distance parcourue | Vitesse moyenne | Vitesse maximale | Dénivelé Positif | FC Moyenne | Energie totale produite | PPO | FTP | VO2MAX |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| {str(datetime.timedelta(seconds=int(self.data["time_delta"].sum())))} | {self.data["position"].max()/1000:.2f} km | {self.data["position"].max()/self.data["time_delta"].sum()*3.6:.1f} km/h | {self.data["speed"].max()*3.6:.0f} km/h | {self.data[self.data["altitude_delta"]>0]["altitude_delta"].sum():.0f} m | {self.data["heart_rate"].mean():.0f} bpm | {(self.data["watts"]*self.data["time_delta"]).sum()/1000:.0f} kJ | {self.estimate_ppo():.0f} W | {self.estimate_ftp()/self.mass:.1f} W/kg | {self.estimate_vo2max():.0f} mL/kg/min |

</center>
            
        """))
    
    def show_map(self):
        df = self.get_data()
        df = df.reset_index(drop=True)
        
        lat_center = df['lat'].mean()
        lon_center = df['lon'].mean()
        
        m = folium.Map(location=[df['lat'].iloc[0],df["lon"].iloc[1]],zoom_start=18,tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', attr='Esri')
        
        # add lines of the stage depending on slope
        cmap = LinearSegmentedColormap.from_list(
            "mycmap", [(0, "green"), (0.03, "yellow"), (0.06, "orange"), (0.09, "red"), (0.12, "brown"), (0.15, "black"), (1,"black")]
        )
        
        def get_rolling_averager(window):
            """
            Args:
                window (float): returns a function to compute the avg slope over <window> meters
            """
            def get_avg_slope(row):
                
                return df[
                    (df["position"]>row["position"]-window/2) & (df["position"]<row["position"]+window/2)
                ]["slope"].mean()
                
            return get_avg_slope
                
        df['slope'] = df.apply(get_rolling_averager(100),axis=1)
        
        df['color'] = df['slope'].apply(lambda x: colors.to_hex(cmap(abs(x))))
        
        for i in range(0, len(df)-1):
            folium.PolyLine([df.loc[i, ['lat', 'lon']].values, df.loc[i+1, ['lat', 'lon']].values], color=df.loc[i, 'color']).add_to(m)
        #folium.PolyLine(df[['lat', 'lon']].values).add_to(m)
        
        folium.Circle(
            radius=10,
            location=[df['lat'].iloc[0], df['lon'].iloc[0]],
            popup='START',
            color="purple",
            fill=True
        ).add_to(m)
        
        folium.Circle(
            radius=10,
            location=[df['lat'].iloc[-1], df['lon'].iloc[-1]],
            popup='END',
            color="blue",
            fill=True
        ).add_to(m)
        
        folium.Marker()
        
        display(HTML(f'<div style="width:50vw;margin:auto;{m._repr_html_()}</div>'))
        
        legend_html = """
<div style="width:100%; fontsize:14px; display:flex; align-items:center; flex-direction:column;">
    <div>
    <b>Legende (pente moyenne sur 100m)</b><br>
    <div style="background: green; width: 10px; height: 10px; display: inline-block;"></div> 0%-3%<br>
    <div style="background: yellow; width: 10px; height: 10px; display: inline-block;"></div> 3%-6%<br>
    <div style="background: orange; width: 10px; height: 10px; display: inline-block;"></div> 6%-9%<br>
    <div style="background: red; width: 10px; height: 10px; display: inline-block;"></div> 9%-12%<br>
    <div style="background: brown; width: 10px; height: 10px; display: inline-block;"></div> 12%-15%<br>
    <div style="background: black; width: 10px; height: 10px; display: inline-block;"></div> >15%
    </div>
</div>
"""
        display(HTML(legend_html))

    def show_profile(self):
        fig,ax = plt.subplots(figsize=(20,4))
        
        ax.set_title("Profile du parcours")
        X = self.set_x_axis(ax,"distance")
        Y = self.set_y_axis(ax,"altitude")
        
        ax.plot(X,Y,color="purple")
        ax.fill_between(X,Y,color="purple")
         
        plt.show()
    
    
    def show_speed(self):
        fig,ax = plt.subplots(figsize=(20,4))
        
        ax.set_title("Vitesse instantanée du cycliste")
        X = self.set_x_axis(ax,"time")
        Y = self.set_y_axis(ax,"speed")
        
        ax.bar(X,Y,color="red",width=self.data["time_delta"]*2)
        
        ax.axhline(y=Y.max(),color="grey",linestyle="--",label=f"Vitesse Maximale : {Y.max():.0f} km/h")
        ax.legend()
         
        plt.show()
    
    def show_slope(self):
        fig,ax = plt.subplots(figsize=(20,4))
        
        ax.set_title("Pente positive moyennée")
        X = self.set_x_axis(ax,"distance")
        self.set_y_axis(ax,"slope")
        
        # recompute slope to make a mean over 100m
        data = self.get_data()
        data.loc[data["slope"]<0,"slope"]=0
        
        def get_rolling_averager(window):
            """
            Args:
                window (float): returns a function to compute the avg slope over <window> meters
            """
            def get_avg_slope(row):
                
                return data[
                    (data["position"]>row["position"]-window/2) & (data["position"]<row["position"]+window/2)
                ]["slope"].mean()*100
                
            return get_avg_slope
                
        Y1 = data.apply(get_rolling_averager(100),axis=1)
        Y2 = data.apply(get_rolling_averager(1000),axis=1)

        ax.plot(X,Y1,color="lightblue",label="Pente moyenne sur 100m")
        ax.plot(X,Y2,color="darkblue",label="Pente moyenne sur 1000m")
        
        # find maximum
        ax.axhline(y=Y1.max(),color="lightgrey",linestyle="--",label=f"Pente moyenne maximale sur 100m : {Y1.max():.1f}%")
        ax.axhline(y=Y2.max(),color="darkgrey",linestyle="--",label=f"Pente moyenne maximale sur 1000m : {Y2.max():.1f}%")
        
        ax.set_ylim(0,Y1.max()*1.3)
        ax.legend()
        plt.show()
    
    
    #######################
    # PLOTS (PERFORMANCE) #
    #######################
    
    def show_cardiac_frequency(self):
        
        if self.data["heart_rate"].max()<1:
            print("No heart rate data available")
            return
        
        fig,ax = plt.subplots(figsize=(20,4))
        
        ax.set_title("Fréquence cardiaque")
        X = self.set_x_axis(ax,"time")
        Y = self.set_y_axis(ax,"heart_rate")
        
        ax.axhline(y=Y.max(),color="grey",linestyle="--",label=f"Fréquence cardiaque maximale : {Y.max():.0f} bpm")
        ax.plot(X,Y,color="green")
        ax.legend()
        plt.show()
    
    def show_watts(self):
        fig,ax = plt.subplots(figsize=(20,4))
        
        ax.set_title("Estimation de la puissance développée")
        X = self.set_x_axis(ax,"time")
        self.set_y_axis(ax,"watts")
        
        data = self.get_data().set_index("time")
        
        Y1 = data["watts"].rolling(window='1min',min_periods=1).mean()
        Y2 = data['watts'].rolling(window='20min',min_periods=1).mean()
        
        ppo = self.estimate_ppo()
        ftp = self.estimate_ftp()
        ax.axhline(y=ppo,color="lightgrey",linestyle="--",label=f"Peak Power Input (PPO) : {ppo:.0f} W")
        ax.axhline(y=ftp,color="darkgrey",linestyle="--",label=f"Fonctional Treshold Power (FTP) : {ftp:.0f} W")
        ax.plot(X,Y1,color="yellow",label="Puissance moyenne sur 1min")
        ax.plot(X,Y2,color="orange",label="Puissance moyenne sur 20min")
        ax.set_ylim(0,Y1.max()*1.1)
        ax.legend()
        plt.show()
    
    def show_efficiency(self):
        if self.get_data()["heart_rate"].max()<1:
            print("No heart rate data available")
            return
        
        fig = plt.figure(figsize=(20,8))
        ax,ax2 = fig.subplots(2,1)
        
        # AX1
        ax.set_title("Correlation Puissance-FC")
        
        data = self.get_data().set_index("time")[["watts","heart_rate"]]
        minutes = 2
        data = data.rolling(window=f"{minutes}min",min_periods=self.min_periods(minutes)).mean()
        data = data.reset_index()
        data["activity_time"] = self.get_data()["activity_time"]
        data = data[~data["watts"].isna()]
        data["w_fc"] = data["watts"]/data["heart_rate"]
        
        def stdz(target:pd.Series):
            return (target-target.mean())/target.std()
        
        X = data["activity_time"]
        Y1 = stdz(data["watts"])
        Y2 = stdz(data["heart_rate"])
        
        x_ticks = np.linspace(X.min(),X.max(),10)
        ax.set_xticks(
            x_ticks,
            labels=[f"{str(datetime.timedelta(seconds=int(x)))}" for x in x_ticks]
        )
        
        ax.plot(X,Y1,color="orange",label="Puissance (anomalie)")
        ax.plot(X,Y2,color="green",label="Fréquence cardiaque (anomalie)")
        ax.set_xlabel("Temps")
        ax.set_ylabel("Écart à la moyenne\n(écart-type)")
        ax.legend()
        
        # AX2
        ax2.set_xlabel("Temps")
        ax2.set_ylabel("Efficacité (W/BPM)")
        ax2.plot(X,data["w_fc"])
        
        high_bpm_avg = data[(stdz(data["heart_rate"])>0.5) & (stdz(data["watts"])>0.5)]["w_fc"].mean()
        
        ax2.set_xticks(
            x_ticks,
            labels=[f"{str(datetime.timedelta(seconds=int(x)))}" for x in x_ticks]
        )
        
        ax2.axhline(y=high_bpm_avg,color="grey",linestyle="--",label=f"Efficacité moyenne en effort intense : {high_bpm_avg:.2f} W/BPM")
        
        ax2.legend()
        plt.show()
        
        
        
    ###########################
    # PERFORMANCE ESTIMATIONS #
    ###########################
    
    def estimate_ftp(self)->float:
        """
        FTP = 95% of the maximal power output averaged over 20 minutes

        Returns:
            FTP: (W)
        """
        data = self.get_data().set_index('time')
        return data["watts"].rolling(window='20min',min_periods=self.min_periods(20)).mean().max()*0.95 # we consider on average 5 seconds per step, divide by two in case values are missing
    
    
    def estimate_ppo(self)->float:
        """
        Peak Power Output = maximal power output avg over 150s
        Returns:
            PPO: (W)
        """
        data = self.get_data().set_index('time')
        return data["watts"].rolling(window='150s',min_periods=self.min_periods(seconds=150)).mean().max()

    def estimate_vo2max(self)->float:
        """

        Returns:
            VO_{2 max}: (mL/kg/min)
        """
        return (0.01141*self.estimate_ppo() + 0.435) * 1_000 / self.mass
        
        
    ###########
    # PHYSICS #
    ###########
    
    def _overwrite_altitude_with_ign(self):
        """
        Data:
            https://geoservices.ign.fr/documentation/services/api-et-services-ogc/calcul-altimetrique-rest (API)
            
        Action:
            overwrites the altitude data with data from the IGN website, to avoid inprecise data from the Garmin GPS mesures
        """
        def get_altitude_profile(lons:pd.Series,lats:pd.Series):
            url = "https://wxs.ign.fr/calcul/alti/rest/elevationLine.json"
            params = {
                'lon': "|".join(map(str, lons)),
                'lat': "|".join(map(str, lats)),
            }
            response = requests.get(url, params=params)
            data = json.loads(response.text)
            return data['elevations']
        
        def get_altitude(lons:pd.Series,lats:pd.Series, window:int=190):
            """
            Args:
                window (int, optional): cannot send thausands of points to the website. We have to cut it down. Defaults to 190.
            """
            altitudes = []
            start_index=0
            while start_index < len(lons):
                stop_index = min(len(lons),start_index+window)
                altitudes.extend(get_altitude_profile(lons[start_index:stop_index],lats[start_index:stop_index]))
                start_index+=window
            return [mesure["z"] for mesure in altitudes]

        self.data["altitude"] = get_altitude(self.data["lon"],self.data["lat"])
        self.data["altitude"] = self.data["altitude"].rolling(window=5,min_periods=1).median()
        print("Altitude data overwritten with IGN data")
    
    @staticmethod
    def compute_drag(
        mass:float,
        size:float,
        speed:float,
        altitude:float
    ):
        """
        Arguments can be passed as pd.DataSeries too
        
        Args:
            mass (float): kg
            size (float): m
            speed (float): m/s
            altitude (float): m
        """
        assert size<5,f"Size should be given in meters, and to my knowledge a man cannot be {size}m tall "
        projected_frontal_area = 0.0293 * (size**0.725) * (mass**0.425) + 0.0604
        rho = CyclingData.rho0 * (1-CyclingData.L*altitude/CyclingData.T0) ** (CyclingData.g/(CyclingData.R*CyclingData.L)-1)
        kinetic_pressure = 0.5 * rho * speed**2
        
        return CyclingData.drag_coeffictient * projected_frontal_area * kinetic_pressure
    
    @staticmethod
    def set_cyclist(mass:float,size:float):
        """
        Args:
            mass (kg)
            size (m)
        """
        assert size < 5 ,"Size must be given in meters!"
        CyclingData.mass = mass
        CyclingData.size = size
    
    @staticmethod
    def _show_file_structure(filename:str):
        file = FitFile("activites/"+filename)
        for mesure in file.get_messages('record'):
            for mesure_column in mesure:
                print(f"Mesure : {mesure_column.name} -> {mesure_column.value}")
            break
    

        
        