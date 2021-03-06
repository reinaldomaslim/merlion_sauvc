#!/usr/bin/env python

import rospy
import math
import cv2
from cv_bridge import CvBridge
import numpy as np
from geometry_msgs.msg import Pose, Point, Quaternion, Twist, PoseArray
from sensor_msgs.msg import PointCloud2, Image
from nav_msgs.msg import Odometry
from tf.transformations import euler_from_quaternion, quaternion_from_euler
from visualization_msgs.msg import MarkerArray, Marker

import time
import random


#################################
##############class##############
#################################

class DetectGate(object):
    forward_speed=1.5
    side_speed=1.5
    dive_speed=0.8
    yaw_speed=1
    yaw_count=0

    x0, y0, z0=0, 0, 0
    roll0, pitch0, yaw0=0, 0, 0
    odom_received=False

    #make birdeye heatmap with size 50, 25, ppm=2, init_pos=0.7, 25 
    birdeye_heatmap=np.zeros((50, 100), dtype=np.uint8)
    init_pos=0.7, 25
    ppm=2

    def __init__(self, nodename, drive=None):
        rospy.init_node(nodename, anonymous=False)
        self.bridge = CvBridge()
        self.init_markers()
        rospy.Subscriber("/logi_c920/usb_cam_node/image_raw", Image, self.img_callback, queue_size = 1)
        rospy.Subscriber("/front/image_rect_color", Image, self.img_callback, queue_size = 1)


        rospy.Subscriber('/visual_odom', Odometry, self.odom_callback, queue_size=1)
        # while self.odom_received!=True and not rospy.is_shutdown():
        #     rospy.sleep(1)
        #     print("waiting for odom from predict_height")

        self.img_pub=rospy.Publisher('/gate_img', Image, queue_size=1)
        self.birdeye_heatmap_pub=rospy.Publisher('/birdeye_gate', Image, queue_size=1)
        self.cmd_vel_pub=rospy.Publisher('/merlion/control/cmd_vel', Twist, queue_size=1)

        rate=rospy.Rate(10)

        while not rospy.is_shutdown():
            rate.sleep()


    def img_callback(self, msg):
        font = cv2.FONT_HERSHEY_SIMPLEX
        color=(0, 0, 255)
        start_time=time.time()
        img=self.bridge.imgmsg_to_cv2(msg, "bgr8")
        # img=self.img_correction(img)
        
        res=img.copy()
        blur = cv2.GaussianBlur(img,(7, 7),0)
        grey=cv2.cvtColor(blur, cv2.COLOR_BGR2GRAY)


        hsv=cv2.cvtColor(blur, cv2.COLOR_BGR2HSV)
        mask = cv2.adaptiveThreshold(hsv[:, :, 2],255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,\
                    cv2.THRESH_BINARY,21, 2)
        kernel = np.ones((5,5),np.uint8)    
        opening = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        opening=255-opening
        opening = cv2.erode(opening, None, iterations=1)
        

        # cv2.fastNlMeansDenoising(opening, opening, 7, 21, 3)

        # opening=self.dbscan(opening)

        minLineLength=70
        lines = cv2.HoughLinesP(image=opening,rho=1,theta=np.pi/180,\
         threshold=80,lines=np.array([]), minLineLength=minLineLength,maxLineGap=12)

        heatmap=np.zeros_like(opening)
        side=10
        try:
            h_lines=[]
            v_lines=[]

            for line in lines:
                
                x1, y1, x2, y2=line[0][0], line[0][1], line[0][2], line[0][3]
                
                theta=abs(math.atan(float(y2-y1)/(x2-x1+0.001))*180/math.pi)
                # print(theta)
                angle_thres=30
                if theta<angle_thres:
                    #horizontal
                    cv2.line(res, (x1, y1), (x2, y2), (0, 0, 255), 3, cv2.LINE_AA)
                    h_lines.append(np.array([[x1, y1], [x2, y2]]))
                elif abs(theta-90)<angle_thres:
                    #vertical
                    cv2.line(res, (x1, y1), (x2, y2), (0, 255, 0), 3, cv2.LINE_AA)
                    v_lines.append(np.array([[x1, y1], [x2, y2]]))

            if len(h_lines)>0 and len(v_lines)>0:
                crosses=self.find_crosses(h_lines, v_lines)
                
                for cross, center, depth in crosses:
                    cv2.circle(res, (int(cross[0]), int(cross[1])), 10, (255, 0, 0), -1)
                    cv2.circle(res, (int(center[0]), int(center[1])), 20, (170, 0, 170), -1)
                    heatmap[center[1]-side:center[1]+side, center[0]-side:center[0]+side]+=1
                    text="distance: "+str(round(depth, 2)) +"m"
                    cv2.putText(res, text, (int(cross[0])+10, int(cross[1])-20), font, 0.5, color, 1, cv2.LINE_AA)

            #process heatmap
            gate=(np.argmax(heatmap)%img.shape[1], int(math.floor(np.argmax(heatmap)/img.shape[1])+1))
            max_val=np.amax(heatmap)
            if max_val>0:
                heatmap_img = cv2.applyColorMap(heatmap*int(255/max_val), cv2.COLORMAP_JET)
            else:
                heatmap_img = cv2.applyColorMap(heatmap, cv2.COLORMAP_JET)
            #publish velocity command  
            msg=Twist()
            # print(gate)

            pt1=(int(opening.shape[1]/2), int(opening.shape[0]/2))
            if max_val==0:
                #stop
                msg.linear.x=0
                msg.linear.y=0
            elif gate[0]==0 and gate[1]==1:
                #gate not found, yaw to search
                msg.angular.z=(-1)**(int(self.yaw_count/10)%2)*self.yaw_speed
                self.yaw_count+=1
            elif abs(gate[1]-opening.shape[0]/2.0)/opening.shape[0]>0.2:
                #adjust height 
                sign=abs(gate[1]-opening.shape[0]/2.0)/(gate[1]-opening.shape[0]/2.0)
                #move sideway
                msg.linear.z=-1.0*sign*self.dive_speed
                pt2=(pt1[0], pt1[1]+int(sign*100))
                cv2.arrowedLine(res, pt1, pt2, (0,230,235), 5)
            elif abs(gate[0]-opening.shape[1]/2.0)/opening.shape[1]<0.1:
                #move forward
                msg.linear.x=self.forward_speed
                pt2=(pt1[0], pt1[1]-30)
                cv2.arrowedLine(res, pt1, pt2, (0,0,255), 5)
            else:
                sign=abs(gate[0]-opening.shape[1]/2.0)/(gate[0]-opening.shape[1]/2.0)
                #move sideway
                msg.linear.y=-1.0*sign*self.side_speed
                pt2=(pt1[0]+int(sign*100), pt1[1])
                cv2.arrowedLine(res, pt1, pt2, (255,0,0), 5)
            

            
            self.cmd_vel_pub.publish(msg)
            opening=cv2.cvtColor(opening, cv2.COLOR_GRAY2BGR)
            fin = cv2.addWeighted(heatmap_img, 1, opening, 1, 0)
            self.img_pub.publish(self.bridge.cv2_to_imgmsg(np.hstack([fin, res]), "bgr8"))

            #compute real position of gate in x,y,z
            #first x and y refers to side and height
            fov_w, fov_h=62*math.pi/180, 46*math.pi/180
            px_W, px_H=640, 480

            pd=(px_W/2)/math.tan(fov_w/2)
            del_px=(-gate[0]+opening.shape[1]/2.0)
            del_py=(-gate[1]+opening.shape[0]/2.0)
            del_x=depth*del_px/pd
            del_y=depth*del_py/pd

            del_real_x=del_x*math.cos(self.roll0)-del_y*math.sin(self.roll0)
            del_real_y=del_x*math.sin(self.roll0)+del_y*math.cos(self.roll0)+depth*math.tan(self.pitch0)
            
            x_gate=self.x0+depth*math.cos(self.yaw0)-del_real_x*math.sin(self.yaw0)
            y_gate=self.y0+depth*math.sin(self.yaw0)+del_real_x*math.cos(self.yaw0)
            z_gate=self.z0+del_real_y
            
            #plot in map
            ind_x=int(self.birdeye_heatmap.shape[0]-(self.init_pos[0]+x_gate)*self.ppm)
            ind_y=int((self.init_pos[1]-y_gate)*self.ppm)
            self.birdeye_heatmap[ind_x, ind_y]+=1

            max_val=np.amax(self.birdeye_heatmap)
            if max_val>0:
                birdeye_heatmap_img = cv2.applyColorMap(self.birdeye_heatmap*int(255/max_val), cv2.COLORMAP_JET)
            else:
                birdeye_heatmap_img = cv2.applyColorMap(self.birdeye_heatmap, cv2.COLORMAP_JET)

            ind=np.argmax(self.birdeye_heatmap)
            ind_x=int(ind/self.birdeye_heatmap.shape[1])
            ind_y=ind%self.birdeye_heatmap.shape[1]
            gate_pos=[]
            gate_pos.append((self.birdeye_heatmap.shape[0]-ind_x)/self.ppm-self.init_pos[0])
            gate_pos.append(self.init_pos[1]-ind_y/self.ppm)
            
            self.printMarker(gate_pos)
            self.birdeye_heatmap_pub.publish(self.bridge.cv2_to_imgmsg(birdeye_heatmap_img, "bgr8"))

        except:
            pass

        

    def predict_depth(self, line1, line2):
        fov_w, fov_h=62*math.pi/180, 46*math.pi/180
        px_W, px_H=640, 480
        # print(line1, line2)
        # print(np.subtract(line1[0, :], line1[1,:]))
        l1=np.sqrt(np.sum(np.square(np.subtract(line1[0, :], line1[1,:])), axis=0))
        l2=np.sqrt(np.sum(np.square(np.subtract(line2[0, :], line2[1,:])), axis=0))
        # print(l1, l2)
        if abs(l2-l1)/l2<0.3:
            l=(l1+l2)/2
        elif abs(l2-l1)/l2<0.5:
            l=max(l1, l2)
        else:
            #not pole
            return -1
        #real length of pole in metres
        real_l=1.5
        ppm=l/real_l
        H=px_H/ppm
        depth=H/(2*math.tan(fov_h/2))
        # print(depth)
        return depth


    def find_crosses(self, h_lines, v_lines):
        crosses=[]
        for h in h_lines:
            h2=h.copy()
            h2[[0, 1]]=h2[[1, 0]]
            h=np.concatenate((h, h2), axis=0)
            for v in v_lines:
                v=np.concatenate((v, v), axis=0)
                #find if they have near corners, four combinations
                d=np.sqrt(np.sum(np.square(h-v), axis=1))
                val=np.amin(d)
                if val<20:
                    ind=np.argmin(d)
                    
                    cross=np.add(h[ind], v[ind])/2
                    center=np.add(h[(ind+2)%4], v[(ind+1)%4])/2
                    depth=self.predict_depth(h[0:2,:], v[0:2, :])

                    if center[1]>cross[1] and depth>0:
                        crosses.append((cross, center, depth))
        # print(crosses)
        return crosses


    def get_start_end(self, hist):
        
        diff=hist[1:len(hist)]-hist[0:len(hist)-1]
        # diff=np.sort(diff)
        start=np.argmax(diff)
        # print(diff[0], diff[1])

        return start, 0

    def img_correction(self, img):
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(6, 6))
        res=np.zeros_like(img)
        for i in range(3):
            res[:, :, i] = clahe.apply(img[:, :, i])
        return res


    def odom_callback(self, msg):
        self.x0 = msg.pose.pose.position.x
        self.y0 = msg.pose.pose.position.y
        self.z0 = msg.pose.pose.position.z
        # print(self.z0)
        self.roll0, self.pitch0, self.yaw0 = euler_from_quaternion((msg.pose.pose.orientation.x, msg.pose.pose.orientation.y, msg.pose.pose.orientation.z, msg.pose.pose.orientation.w))
        self.odom_received = True
        


    def printMarker(self, gate_pos):
                #markerList store points wrt 2D world coordinate
                
        self.markers.points=[]
        p=Point()

        self.markers.points.append(Point(0, 0, 0))
        p.x=gate_pos[0]
        p.y=gate_pos[1]
        p.z=0.75
        q_angle = quaternion_from_euler(0, 0, 0)
        q = Quaternion(*q_angle)
        self.markers.pose = Pose(p, q)

        self.marker_pub.publish(self.markers)


    def init_markers(self):
        # Set up our waypoint markers
        marker_scale = 0.2
        marker_lifetime = 0  # 0 is forever
        marker_ns = 'frontiers'
        marker_id = 0
        marker_color = {'r': 1.0, 'g': 0.7, 'b': 1.0, 'a': 1.0}

        # Define a marker publisher.
        self.marker_pub = rospy.Publisher('gate_markers', Marker, queue_size=5)

        # Initialize the marker points list.
        self.markers = Marker()
        self.markers.ns = marker_ns
        self.markers.id = marker_id
        # self.markers.type = Marker.ARROW
        self.markers.type = Marker.SPHERE_LIST
        self.markers.action = Marker.ADD
        self.markers.lifetime = rospy.Duration(marker_lifetime)
        self.markers.scale.x = marker_scale
        self.markers.scale.y = marker_scale
        self.markers.scale.z = marker_scale
        self.markers.color.r = marker_color['r']
        self.markers.color.g = marker_color['g']
        self.markers.color.b = marker_color['b']
        self.markers.color.a = marker_color['a']

        self.markers.header.frame_id = 'map'
        self.markers.header.stamp = rospy.Time.now()
        self.markers.points = list()

##########################
##########main############
##########################



if __name__ == '__main__':

    #load darknet
    # model.load_weights('/media/ml3/Volume/image-to-3d-bbox/weights.h5')

    try:
        DetectGate(nodename="detect_gate", drive=None)
    except rospy.ROSInterruptException:
        rospy.loginfo("finished.")
