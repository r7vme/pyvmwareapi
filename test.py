#!/usr/bin/env python2

from driver import VMwareESXDriver

if __name__=="__main__":
    esxi = VMwareESXDriver('172.18.210.165','root','Mirantis01')
    network_info = [{'address':'00:11:22:33:44:55', 'pg':'pgtest', 'vlan':'400'}]
    instance = {'name':'testvm','vcpus':'1','memory_mb':'1024'}
    disk_size = 10737418240
    esxi.spawn(instance, disk_size, network_info)
