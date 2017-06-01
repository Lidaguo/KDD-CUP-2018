# coding=utf-8
'''
在volume_predict的基础上改进模型

建模思路：
创建训练集，总的要求就是以前两个小时数据为训练集，用迭代式预测方法
例如8点-10点的数据预测10点20,8点-10点20预测10点40……，每一次预测使用的都是独立的（可能模型一样）的模型
现在开始构建训练集
第一个训练集特征是所有两个小时（以20分钟为一个单位）的数据，因变量是该两小时之后20分钟的流量
第二个训练集，特征是所有两个小时又20分钟（以20分钟为一个单位）的数据，因变量是该两个小时之后20分钟的流量
以此类推训练12个GBDT模型，其中entry 6个，exit 6个

待优化思路：
1. 该模型想说明的问题是当前待预测时段的车流只和之前两小时车流有线性（或非线性）关系，这个认识其实比较局限，可以尝试
   换一个角度思考，当前时刻的车流量也可能和之前一个月同一时段车流量呈线性（或非线性）关系

2. 如何证明分开考虑收费站比将收费站全部整合到一起效果好，如果将收费站整合到一起的话，那么就不对收费站id，出入方向做分类

3. 时间序列相似度不靠谱，将所有特征丢到GBDT模型中训练，从特征评分中我们发现：当前客流还是和之前客流有非常大的相关性，而
   日期只是个简单的调整参数，我感觉这是因为训练集规模小导致了某些特征被弱化。因此把所有特征都丢进GBDT里不能单独体现时间
   对车流的影响。解决办法有两种：第一种是使用特征权重，这种方法暂时不太会用，而且权重值大小是个问题；第二种是单纯将时间
   特征提取出来，这样得到的GBDT相当于考虑了大量的时间因素。再将两种模型线性结合（或者stacking）结合得到最终结果

4. 可以用样本自相关函数判断下一时段的车流到底和之前几个20分钟线性相关（因为不一定要前2个小时来预测下一个20分钟），样本自
   相关函数用statsmodel库（感觉不一定靠谱）

6. 尝试使用神经网络建模

7. 平方，开方，开更只是对线性模型有用，对GBDT没有任何用处，甚至对RF，ET这种随机性很强的模型有误导作用（因为平方，开方等
  操作在树结构模型中相当于改变了原始特征的分布，将平方，开方的特征概率提高）

优化思路：
1. 根据题目所给评价函数，如果将y转换成log(y)，那么损失函数可以朝lad方向梯度下降（过程已经大致证明了），而特征的log处理
   不影响CART的回归结果，所以对所有车流量（不论特征还是因变量都做log计算）。如果使用其他非树形结构模型需要考虑是否要对
   所有数据做log计算

2. 增加特征，之前只考虑20分钟内的车流量情况，现在加上在20分钟内的总载重量，平均载重量；货车数量，货车总载重量，货车平均
   载重量；客车数量，客车总载重量，客车平均载重量；使用电子桩的车数（2个小时6个时段，每个时段有10维特征，一共60维）；
   2小时内总载重量，平均载重量，货车数量，货车总载重量，货车平均载重量，客车数量，客车总载重量，客车平均载重量（9维）；
   。再加上待预测时段的时间信息，包括月，日，时，分

3. 从观察数据得出来的结论，在10月1日附近（具体哪几天不记得了）全天的数据相对于其他日期的数据很像噪声点，可以尝试剔除那
   几天的数据

4. 不需要删除国庆7天的数据，只需要删除国庆和国庆前后连接点的数据就可以了。原因：考虑到删除国庆数据会造成大幅度的过拟合，
   所以删除噪声点可以提高成绩，但是删除的方式需要斟酌。

5. 日期是一种标签含义的数组特征，需要变成字符形，这个只能在stacking文件里做了，predict2不好处理标签特征，因为它的训
   练和预测分开了，而哑编码需要将训练集和预测集放一块

6. 尝试将所有模型用stacking融合

7. 改进hour特征，因为预测集涉及到的hour只有8,9,17,18，所以把训练集的时间也设定为四维特征，其中全0代表除这四个时段以外
   的其他时段

'''

import pandas as pd
import numpy as np
import seaborn as sns
import warnings
import matplotlib.pyplot as plt
from sklearn.ensemble import GradientBoostingRegressor
from pandas.tseries.offsets import *
from sklearn.model_selection import GridSearchCV
from scipy.stats import skew
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LinearRegression, RidgeCV, LassoCV, ElasticNetCV, Ridge, Lasso
from sklearn.cross_validation import KFold
from sklearn.ensemble import ExtraTreesRegressor


# description of the feature:
# Traffic Volume through the Tollgates
# time           datatime        the time when a vehicle passes the tollgate
# tollgate_id    string          ID of the tollgate
# direction      string           0:entry, 1:exit
# vehicle_model  int             this number ranges from 0 to 7, which indicates the capacity of the vehicle(bigger the higher)
# has_etc        string          does the vehicle use ETC (Electronic Toll Collection) device? 0: No, 1: Yes
# vehicle_type   string          vehicle type: 0-passenger vehicle, 1-cargo vehicle

def preprocessing():
    '''
    预处理训练集
    '''
    volume_df = pd.read_csv("train_merge.csv")
    # 替换所有有标签含义的数字
    volume_df['tollgate_id'] = volume_df['tollgate_id'].replace({1: "1S", 2: "2S", 3: "3S"})
    volume_df['direction'] = volume_df['direction'].replace({0: "entry", 1: "exit"})
    volume_df['has_etc'] = volume_df['has_etc'].replace({0: "No", 1: "Yes"})
    volume_df['vehicle_type'] = volume_df['vehicle_type'].replace({0: "passenger", 1: "cargo"})
    volume_df['time'] = volume_df['time'].apply(lambda x: pd.Timestamp(x))

    # 剔除10月1日至10月6日数据（每个收费站在该日期附近都有异常）
    # volume_df = volume_df[(volume_df["time"] < pd.Timestamp("2016-10-01 00:00:00")) |
    #                       (volume_df["time"] > pd.Timestamp("2016-10-07 00:00:00"))]

    # 承载量：1-默认客车，2-默认货车，3-默认货车，4-默认客车
    # 承载量大于等于5的为货运汽车，所有承载量为0的车都类型不明
    # volume_df = volume_df.sort_values(by="vehicle_model")
    # vehicle_model0 = volume_df[volume_df['vehicle_model'] == 0].fillna("No")
    # vehicle_model1 = volume_df[volume_df['vehicle_model'] == 1].fillna("passenger")
    # vehicle_model2 = volume_df[volume_df['vehicle_model'] == 2].fillna("cargo")
    # vehicle_model3 = volume_df[volume_df['vehicle_model'] == 3].fillna("cargo")
    # vehicle_model4 = volume_df[volume_df['vehicle_model'] == 4].fillna("passenger")
    # vehicle_model5 = volume_df[volume_df['vehicle_model'] >= 5].fillna("cargo")
    # volume_df = pd.concat([vehicle_model0, vehicle_model1, vehicle_model2,
    # vehicle_model3, vehicle_model4, vehicle_model5])
    # volume_df["vehicle_type"] = volume_df["vehicle_type"].fillna("No")

    '''
    处理预测集
    '''
    volume_test = pd.read_csv("../testing_phase1/volume(table 6)_test2.csv")
    volume_test.columns = ["time","tollgate_id","direction","vehicle_model","has_etc","vehicle_type"]
    # 替换所有有标签含义的数字
    volume_test['tollgate_id'] = volume_test['tollgate_id'].replace({1: "1S", 2: "2S", 3: "3S"})
    volume_test['direction'] = volume_test['direction'].replace({0: "entry", 1: "exit"})
    volume_test['has_etc'] = volume_test['has_etc'].replace({0: "No", 1: "Yes"})
    volume_test['vehicle_type'] = volume_test['vehicle_type'].replace({0: "passenger", 1: "cargo"})
    volume_test['time'] = volume_test['time'].apply(lambda x: pd.Timestamp(x))

    # 承载量：1-默认客车，2-默认货车，3-默认货车，4-默认客车
    # 承载量大于等于5的为货运汽车，所有承载量为0的车都类型不明
    # volume_test = volume_test.sort_values(by="vehicle_model")
    # vehicle_model0 = volume_test[volume_test['vehicle_model'] == 0].fillna("No")
    # vehicle_model1 = volume_test[volume_test['vehicle_model'] == 1].fillna("passenger")
    # vehicle_model2 = volume_test[volume_test['vehicle_model'] == 2].fillna("cargo")
    # vehicle_model3 = volume_test[volume_test['vehicle_model'] == 3].fillna("cargo")
    # vehicle_model4 = volume_test[volume_test['vehicle_model'] == 4].fillna("passenger")
    # vehicle_model5 = volume_test[volume_test['vehicle_model'] >= 5].fillna("cargo")
    # volume_test = pd.concat(
    #    [vehicle_model0, vehicle_model1, vehicle_model2, vehicle_model3, vehicle_model4, vehicle_model5])
    # volume_df["vehicle_type"] = volume_df["vehicle_type"].fillna("No")

    return volume_df, volume_test


def modeling():
    volume_train, volume_test = preprocessing()
    result_df = pd.DataFrame()
    tollgate_list = ["1S", "2S", "3S"]
    for tollgate_id in tollgate_list:
        print tollgate_id
        # entry_mean = 0
        # entry_std = 0
        # exit_mean = 0
        # exit_std = 0
        # entry_max = 0
        # entry_min = 0
        # exit_max = 0
        # exit_min = 0

        # 创建之和流量，20分钟跨度有关系的训练集
        def divide_train_by_direction(volume_df, entry_file_path=None, exit_file_path=None):
            # entry
            volume_all_entry = volume_df[
                (volume_df['tollgate_id'] == tollgate_id) & (volume_df['direction'] == 'entry')].copy()
            volume_all_entry['volume'] = 1
            volume_all_entry["etc_count"] = volume_all_entry["has_etc"].apply(lambda x: 1 if x == "Yes" else 0)
            volume_all_entry["etc_model"] = volume_all_entry["etc_count"] * volume_all_entry["vehicle_model"]
            volume_all_entry["no_etc_count"] = volume_all_entry["has_etc"].apply(lambda x: 1 if x == "No" else 0)
            volume_all_entry["no_etc_model"] = volume_all_entry["no_etc_count"] * volume_all_entry["vehicle_model"]
            # entry方向不记录车辆类型，所以比exit少一些特征
            volume_all_entry["model02_count"] = volume_all_entry["vehicle_model"].apply(
                lambda x: 1 if x >= 0 and x <= 2 else 0)
            volume_all_entry["model02_model"] = volume_all_entry["vehicle_model"] * volume_all_entry["model02_count"]
            volume_all_entry["model35_count"] = volume_all_entry["vehicle_model"].apply(
                lambda x: 1 if x >= 3 and x <= 5 else 0)
            volume_all_entry["model35_model"] = volume_all_entry["vehicle_model"] * volume_all_entry["model35_count"]
            volume_all_entry["model67_count"] = volume_all_entry["vehicle_model"].apply(
                lambda x: 1 if x == 6 or x == 7 else 0)
            volume_all_entry["model67_model"] = volume_all_entry["vehicle_model"] * volume_all_entry["model67_count"]
            volume_all_entry.index = volume_all_entry["time"]
            del volume_all_entry["time"]
            del volume_all_entry["tollgate_id"]
            del volume_all_entry["direction"]
            del volume_all_entry["vehicle_type"]
            del volume_all_entry["has_etc"]
            volume_all_entry = volume_all_entry.resample("20T").sum()
            volume_all_entry = volume_all_entry.fillna(0)

            # exit
            volume_all_exit = volume_df[
                (volume_df['tollgate_id'] == tollgate_id) & (volume_df['direction'] == 'exit')].copy()
            if len(volume_all_exit) > 0:
                volume_all_exit["volume"] = 1
                volume_all_exit["etc_count"] = volume_all_exit["has_etc"].apply(lambda x: 1 if x == "Yes" else 0)
                volume_all_exit["etc_model"] = volume_all_exit["etc_count"] * volume_all_exit["vehicle_model"]
                volume_all_exit["no_etc_count"] = volume_all_exit["has_etc"].apply(lambda x: 1 if x == "No" else 0)
                volume_all_exit["no_etc_model"] = volume_all_exit["no_etc_count"] * volume_all_exit["vehicle_model"]
                # 注意！！！！！！！！！！！
                # 只有exit方向才记录车辆类型
                volume_all_exit["cargo_count"] = volume_all_exit['vehicle_type'].apply(
                    lambda x: 1 if x == "cargo" else 0)
                volume_all_exit["passenger_count"] = volume_all_exit['vehicle_type'].apply(
                    lambda x: 1 if x == "passenger" else 0)
                # volume_all_exit["no_count"] = volume_all_exit['vehicle_type'].apply(lambda x: 1 if x == "No" else 0)
                volume_all_exit["cargo_model"] = volume_all_exit["cargo_count"] * volume_all_exit["vehicle_model"]
                volume_all_exit["passenger_model"] = volume_all_exit["passenger_count"] * \
                                                     volume_all_exit["vehicle_model"]
                # 注意！！！！！！！！！！！
                # volume_all_exit["model02_count"] = volume_all_exit["vehicle_model"].apply(
                #     lambda x: 1 if x >= 0 and x <= 2 else 0)
                # volume_all_exit["model35_count"] = volume_all_exit["vehicle_model"].apply(
                #     lambda x: 1 if x >= 3 and x <= 5 else 0)
                # volume_all_exit["model67_count"] = volume_all_exit["vehicle_model"].apply(
                #     lambda x: 1 if x == 6 or x == 7 else 0)
                volume_all_exit.index = volume_all_exit["time"]
                del volume_all_exit["time"]
                del volume_all_exit["tollgate_id"]
                del volume_all_exit["direction"]
                del volume_all_exit["vehicle_type"]
                del volume_all_exit["has_etc"]
                volume_all_exit = volume_all_exit.resample("20T").sum()
                volume_all_exit = volume_all_exit.fillna(0)

                volume_all_exit["cargo_model_avg"] = volume_all_exit["cargo_model"] / volume_all_exit["cargo_count"]
                volume_all_exit["passenger_model_avg"] = volume_all_exit["passenger_model"] / volume_all_exit[
                    "passenger_count"]
                volume_all_exit["vehicle_model_avg"] = volume_all_exit["vehicle_model"] / volume_all_exit["volume"]
                volume_all_exit = volume_all_exit.fillna(0)
            if entry_file_path:
                volume_all_entry.to_csv(entry_file_path, encoding="utf8")
            if exit_file_path:
                volume_all_exit.to_csv(exit_file_path, encoding="utf8")
            return volume_all_entry, volume_all_exit

        # 计算2个小时为单位的特征
        # train_df就是整合后的特征，
        # offset是从index开始偏移多少个单位
        def generate_2hours_features(train_df, offset, file_path=None, has_type=False):
            # 加之前一定要判断空值，不然空值和数字相加还是空
            train_df["vehicle_all_model"] = train_df["vehicle_model0"] + train_df["vehicle_model1"] + \
                                            train_df["vehicle_model2"] + train_df["vehicle_model3"] + \
                                            train_df["vehicle_model4"] + train_df["vehicle_model5"]
            train_df["etc_all_model"] = train_df["etc_model0"] + train_df["etc_model1"] + train_df["etc_model2"] + \
                                        train_df["etc_model3"] + train_df["etc_model4"] + train_df["etc_model5"]
            train_df["etc_all_count"] = train_df["etc_count0"] + train_df["etc_count1"] + train_df["etc_count2"] + \
                                        train_df["etc_count3"] + train_df["etc_count4"] + train_df["etc_count5"]
            train_df["etc_avg_model"] = train_df["etc_all_model"] / train_df["etc_all_count"]
            # print train_df.columns
            if has_type:
                train_df["cargo_all_model"] = train_df["cargo_model0"] + train_df["cargo_model1"] + \
                                              train_df["cargo_model2"] + train_df["cargo_model3"] + \
                                              train_df["cargo_model4"] + train_df["cargo_model5"]
                train_df["passenger_all_model"] = train_df["passenger_model0"] + train_df["passenger_model1"] + \
                                                  train_df["passenger_model2"] + train_df["passenger_model3"] + \
                                                  train_df["passenger_model4"] + train_df["passenger_model5"]
                train_df["cargo_all_count"] = train_df["cargo_count0"] + train_df["cargo_count1"] + \
                                              train_df["cargo_count2"] + train_df["cargo_count3"] + \
                                              train_df["cargo_count4"] + train_df["cargo_count5"]
                train_df["passenger_all_count"] = train_df["passenger_count0"] + train_df["passenger_count1"] + \
                                                  train_df["passenger_count2"] + train_df["passenger_count3"] + \
                                                  train_df["passenger_count4"] + train_df["passenger_count5"]
                train_df["cargo_avg_model"] = train_df["cargo_all_model"] / train_df["cargo_all_count"]
                train_df["passenger_avg_model"] = train_df["passenger_all_model"] / train_df["passenger_all_count"]
            else:
                train_df["model02_all_count"] = train_df["model02_count0"] + train_df["model02_count1"] + \
                                                train_df["model02_count2"] + train_df["model02_count3"] + \
                                                train_df["model02_count4"] + train_df["model02_count5"]
                train_df["model35_all_count"] = train_df["model35_count0"] + train_df["model35_count1"] + \
                                                train_df["model35_count2"] + train_df["model35_count3"] + \
                                                train_df["model35_count4"] + train_df["model35_count5"]
                train_df["model67_all_count"] = train_df["model67_count0"] + train_df["model67_count1"] + \
                                                train_df["model67_count2"] + train_df["model67_count3"] + \
                                                train_df["model67_count4"] + train_df["model67_count5"]
                train_df["model02_all_model"] = train_df["model02_model0"] + train_df["model02_model1"] + \
                                                train_df["model02_model2"] + train_df["model02_model3"] + \
                                                train_df["model02_model4"] + train_df["model02_model5"]
                train_df["model35_all_model"] = train_df["model35_model0"] + train_df["model35_model1"] + \
                                                train_df["model35_model1"] + train_df["model35_model2"] + \
                                                train_df["model35_model3"] + train_df["model35_model4"]
                train_df["model67_all_model"] = train_df["model67_model0"] + train_df["model67_model1"] + \
                                                train_df["model67_model2"] + train_df["model67_model3"] + \
                                                train_df["model67_model4"] + train_df["model67_model5"]
                train_df["model02_avg_model"] = train_df["model02_all_model"] / train_df["model02_all_count"]
                train_df["model35_avg_model"] = train_df["model35_all_model"] / train_df["model35_all_count"]
                train_df["model67_avg_model"] = train_df["model67_all_model"] / train_df["model67_all_count"]
                train_df = train_df.fillna(0)

            train_df["volume_all"] = train_df["volume0"] + train_df["volume1"] + train_df["volume2"] + \
                                     train_df["volume3"] + train_df["volume4"] + train_df["volume5"]
            train_df["vehicle_avg_model"] = train_df["vehicle_all_model"] / train_df["volume_all"]
            # 二次方 三次方 开方 运算
            # train_df["vehicle_all_model_avg_S2"] = train_df["vehicle_all_model_avg"] * train_df["vehicle_all_model_avg"]
            # train_df["vehicle_all_model_avg_S3"] = train_df["vehicle_all_model_avg"] * \
            #                                        train_df["vehicle_all_model_avg"] * train_df["vehicle_all_model_avg"]
            # train_df["vehicle_all_model_avg_sqrt"] = np.sqrt(train_df["vehicle_all_model_avg"])
            # train_df["vehicle_model_avg5_S2"] = train_df["vehicle_model_avg5"] * train_df["vehicle_model_avg5"]
            # train_df["vehicle_model_avg5_S3"] = train_df["vehicle_model_avg5"] * \
            #                                     train_df["vehicle_model_avg5"] * train_df["vehicle_model_avg5"]
            # train_df["vehicle_model_avg5_sqrt"] = np.sqrt(train_df["vehicle_model_avg5"])
            # train_df["vehicle_model_avg4_S2"] = train_df["vehicle_model_avg4"] * train_df["vehicle_model_avg4"]
            # train_df["vehicle_model_avg4_S3"] = train_df["vehicle_model_avg4"] *\
            #                                     train_df["vehicle_model_avg4"] * train_df["vehicle_model_avg4"]
            # train_df["vehicle_model_avg4_sqrt"] = np.sqrt(train_df["vehicle_model_avg4"])
            # train_df["vehicle_model_avg3_S2"] = train_df["vehicle_model_avg3"] * train_df["vehicle_model_avg3"]
            # train_df["vehicle_model_avg3_S3"] = train_df["vehicle_model_avg3"] * \
            #                                     train_df["vehicle_model_avg3"] * train_df["vehicle_model_avg3"]
            # train_df["vehicle_model_avg3_sqrt"] = np.sqrt(train_df["vehicle_model_avg3"])
            # train_df["vehicle_model_avg2_S2"] = train_df["vehicle_model_avg2"] * train_df["vehicle_model_avg2"]
            # train_df["vehicle_model_avg2_S3"] = train_df["vehicle_model_avg2"] * \
            #                                     train_df["vehicle_model_avg2"] * train_df["vehicle_model_avg2"]
            # train_df["vehicle_model_avg2_sqrt"] = np.sqrt(train_df["vehicle_model_avg2"])
            # train_df["vehicle_model_avg1_S2"] = train_df["vehicle_model_avg1"] * train_df["vehicle_model_avg1"]
            # train_df["vehicle_model_avg1_S3"] = train_df["vehicle_model_avg1"] * \
            #                                     train_df["vehicle_model_avg1"] * train_df["vehicle_model_avg1"]
            # train_df["vehicle_model_avg1_sqrt"] = np.sqrt(train_df["vehicle_model_avg1"])
            # train_df["vehicle_model_avg0_S2"] = train_df["vehicle_model_avg0"] * train_df["vehicle_model_avg0"]
            # train_df["vehicle_model_avg0_S3"] = train_df["vehicle_model_avg0"] * \
            #                                     train_df["vehicle_model_avg0"] * train_df["vehicle_model_avg0"]
            # train_df["passenger_all_model_avg_S2"] = train_df["passenger_all_model_avg"] * train_df["passenger_all_model_avg"]
            # train_df["passenger_all_model_avg_S3"] = train_df["passenger_all_model_avg"]\
            #                                          * train_df["passenger_all_model_avg"] * train_df["passenger_all_model_avg"]
            # train_df["no_all_count_S2"] = train_df["no_all_count"] * train_df["no_all_count"]
            # train_df["no_all_count_S3"] = train_df["no_all_count"] * train_df["no_all_count"] * train_df["no_all_count"]
            # train_df["no_all_count_sqrt"] = np.sqrt(train_df["no_all_count"])
            # train_df["no_count4_S2"] = train_df["no_count4"] * train_df["no_count4"]
            # train_df["no_count4_S3"] = train_df["no_count4"] * train_df["no_count4"] * train_df["no_count4"]
            # train_df["no_count4_sqrt"] = np.sqrt(train_df["no_count4"])
            # train_df["volume1_S2"] = train_df["volume1"] * train_df["volume1"]
            # train_df["volume1_S3"] = train_df["volume1"] * train_df["volume1"] * train_df["volume1"]
            # train_df["volume1_sqrt"] = np.sqrt(train_df["volume1"])
            # train_df["no_count5_S2"] = train_df["no_count5"] * train_df["no_count5"]
            # train_df["no_count5_S3"] = train_df["no_count5"] * train_df["no_count5"] * train_df["no_count5"]
            # train_df["no_count5_sqrt"] = np.sqrt(train_df["no_count5"])
            # train_df["volume2_S2"] = train_df["volume2"] * train_df["volume2"]
            # train_df["volume2_S3"] = train_df["volume2"] * train_df["volume2"] * train_df["volume2"]
            # train_df["volume2_sqrt"] = np.sqrt(train_df["volume2"])
            # train_df["volume3_S2"] = train_df["volume3"] * train_df["volume3"]
            # train_df["volume3_S3"] = train_df["volume3"] * train_df["volume3"] * train_df["volume3"]
            # train_df["volume3_sqrt"] = np.sqrt(train_df["volume3"])
            # train_df["volume_all_S2"] = train_df["volume_all"] * train_df["volume_all"]
            # train_df["volume_all_S3"] = train_df["volume_all"] * train_df["volume_all"] * train_df["volume_all"]
            # train_df["volume_all_sqrt"] = np.sqrt(train_df["volume_all"])
            # train_df["volume4_S2"] = train_df["volume4"] * train_df["volume4"]
            # train_df["volume4_S3"] = train_df["volume4"] * train_df["volume4"] * train_df["volume4"]
            # train_df["volume4_sqrt"] = np.sqrt(train_df["volume4"])
            # train_df["volume5_S2"] = train_df["volume5"] * train_df["volume5"]
            # train_df["volume5_S3"] = train_df["volume5"] * train_df["volume5"] * train_df["volume5"]
            # train_df["volume5_sqrt"] = np.sqrt(train_df["volume5"])
            # 2017-05-11
            # train_df = train_df.fillna(0)
            if offset >= 6 and file_path:
                train_df = generate_time_features(train_df, offset, file_path + "offset" + str(offset - 6))
            elif offset >= 6:
                train_df = generate_time_features(train_df, offset)
            elif file_path:
                train_df.to_csv(file_path + ".csv")
            return train_df

        # 在train_df的index基础上加上offset*20分钟的时间特征
        def generate_time_features(data_df, offset, file_path=None):
            time_str_se = pd.Series(data_df.index)
            time_se = time_str_se.apply(lambda x: pd.Timestamp(x))
            time_se.index = time_se.values
            data_df["time"] = time_se + DateOffset(minutes=offset * 20)
            data_df["day"] = data_df["time"].apply(lambda x: x.day)
            data_df["hour"] = data_df["time"].apply(lambda x: x.hour)
            data_df["minute"] = data_df["time"].apply(lambda x: x.minute)
            data_df["week"] = data_df["time"].apply(lambda x: x.dayofweek)
            data_df["weekend"] = data_df["week"].apply(lambda x: 1 if x >= 5 else 0)
            del data_df["time"]
            if file_path:
                data_df.to_csv(file_path + ".csv")
            return data_df

        # 整合每20分钟的特征，并计算以2个小时为单位的特征
        def generate_features(data_df, new_index, offset, has_y=True, file_path=None, has_type=False):
            train_df = pd.DataFrame()
            for i in range(len(data_df) - 6 - offset):
                se_temp = pd.Series()
                # 删除9月和10月交界的数据，就是训练集的X和y所在时间点分别在两个月份的情况
                # month_left = data_df.index[i]
                # month_right = data_df.index[i + 6 + offset]
                # if month_left == 9 and month_right == 10:
                #     continue
                for k in range(6):
                    se_temp = se_temp.append(data_df.iloc[i + k, :].copy())
                if has_y:
                    se_temp = se_temp.append(pd.Series(data_df.iloc[i + 6 + offset, :]["volume"].copy()))
                se_temp.index = new_index
                se_temp.name = str(data_df.index[i])
                train_df = train_df.append(se_temp)
            # 2015-05-11
            # return generate_2hours_features(train_df.dropna(), 6 + offset, file_path)
            return generate_2hours_features(train_df, 6 + offset, file_path, has_type)

        # 生成gbdt模型
        def gbdt_model(train_X, train_y):
            best_rate = 0.1
            best_n_estimator = 3000
            # param_grid = [
            #     {'max_depth': [4], 'min_samples_leaf': [10],
            #      'learning_rate': [best_rate + 0.01 * i for i in range(-2, 4, 1)],
            #      'loss': ['lad'],
            #      'n_estimators': [1000],#[best_n_estimator + i * 200 for i in range(-2, 3, 1)],
            #      'max_features': [0.7]}#[0.7 + i * 0.1 for i in range(4)]}
            # ]
            param_grid = [
                {'max_depth': [3],
                 'min_samples_leaf': [10],
                 'learning_rate': [0.1],
                 'loss': ['lad'],
                 'n_estimators': [3000],
                 'max_features': [1.0]
                 }
            ]

            # 这是交叉验证的评分函数
            def scorer(estimator, X, y):
                predict_arr = estimator.predict(X)
                y_arr = y
                result = (np.abs(1 - np.exp(predict_arr - y_arr))).sum() / len(y)
                return result

            model = GradientBoostingRegressor()
            clf = GridSearchCV(model, param_grid, refit=True, scoring=scorer)

            clf.fit(train_X, train_y)
            print "Best GBDT param is :", clf.best_params_
            return clf.best_estimator_

        # 生成Lasso线性模型
        def Lasso_model(train_X, train_y):
            skewness = train_X.apply(lambda x: skew(x))
            skewness = skewness[abs(skewness) > 0.5]
            # print(str(skewness.shape[0]) + " skewed numerical features to log transform")
            skewed_features = skewness.index
            train_X[skewed_features] = np.log1p(train_X[skewed_features])
            stdSc = StandardScaler()
            train_X = stdSc.fit_transform(train_X)
            lasso = LassoCV(alphas=[0.0001, 0.0003, 0.0006, 0.001, 0.003, 0.006, 0.01, 0.03, 0.06, 0.1,
                                    0.3, 0.6, 1],
                            max_iter=50000, cv=10)
            lasso.fit(train_X, train_y)
            alpha = lasso.alpha_
            print("Best alpha :", alpha)
            return lasso, train_X

        # 生成Ridge线性模型
        def Ridge_model(train_X, train_y):
            skewness = train_X.apply(lambda x: skew(x))
            skewness = skewness[abs(skewness) > 0.5]
            # print(str(skewness.shape[0]) + " skewed numerical features to log transform")
            skewed_features = skewness.index
            train_X[skewed_features] = np.log1p(train_X[skewed_features])
            stdSc = StandardScaler()
            train_X = stdSc.fit_transform(train_X)
            ridge = RidgeCV(alphas=[0.01, 0.03, 0.06, 0.1, 0.3, 0.6, 1, 3, 6, 10, 30, 60])
            ridge.fit(train_X, train_y)
            alpha = ridge.alpha_
            print("Best alpha :", alpha)

            print("Try again for more precision with alphas centered around " + str(alpha))
            ridge = RidgeCV(alphas=[alpha * .6, alpha * .65, alpha * .7, alpha * .75, alpha * .8, alpha * .85,
                                    alpha * .9, alpha * .95, alpha, alpha * 1.05, alpha * 1.1, alpha * 1.15,
                                    alpha * 1.25, alpha * 1.3, alpha * 1.35, alpha * 1.4],
                            cv=10)
            ridge.fit(train_X, train_y)
            alpha = ridge.alpha_
            print("Best alpha :", alpha)
            return ridge, train_X

        # 创建训练集，总的要求就是以前两个小时数据为训练集，用迭代式预测方法
        # 例如8点-10点的数据预测10点20,8点-10点20预测10点40……，每一次预测使用的都是独立的（可能模型一样）的模型
        # 现在开始构建训练集
        # 第一个训练集特征是所有两个小时（以20分钟为一个单位）的数据，因变量是该两小时之后20分钟的流量
        # 第二个训练集，特征是所有两个小时又20分钟（以20分钟为一个单位）的数据，因变量是该两个小时之后20分钟的流量
        # 以此类推训练12个GBDT模型，其中entry 6个，exit 6个
        def generate_models(volume_entry, volume_exit, entry_file_path=None, exit_file_path=None):
            # global entry_mean
            # global entry_std
            # global entry_max
            # global entry_min
            # global exit_mean
            # global exit_std
            # global exit_max
            # global exit_min
            # print "entry : %f + %f + %f + %f" % (entry_mean, entry_std, entry_max, entry_min)
            # print "exit : %f + %f + %f + %f" % (exit_mean, exit_std, exit_max, exit_min)

            old_index_entry = volume_entry.columns
            new_index_entry = []
            for i in range(6):
                new_index_entry += [item + "%d" % (i,) for item in old_index_entry]
            new_index_entry.append("y")

            # 这是用训练集做预测时的评分函数
            def scorer2(estimator, X, y):
                predict_arr = estimator.predict(X)
                result = (np.abs(1 - np.exp(predict_arr - y))).sum()
                return result

            def filter_error(data_df, mean, std, max_value, min_value):
                columns = ["volume" + str(i) for i in range(6)]
                temp_df = data_df.copy()
                for i in range(len(columns)):
                    temp_df = temp_df[(temp_df[columns[i]] > min(mean - 2 * std, min_value)) &
                                        (temp_df[columns[i]] < max(mean + 2 * std, max_value))]
                return temp_df

            def filter_error2(data_df):
                temp_df = data_df.copy()
                mean_value = temp_df["y"].mean()
                std_value = temp_df["y"].std()
                temp_df = temp_df[(temp_df["y"] > 3) & (temp_df["y"] < mean_value + 2 * std_value)]
                return temp_df

            def filter_error3(data_df):
                temp_df = data_df.copy()
                temp_df["time"] = temp_df.index
                temp_df["time"] = temp_df["time"].apply(pd.Timestamp)
                temp_df = temp_df[(temp_df["time"] < pd.Timestamp("2016-09-30 22:20:00")) |
                              (temp_df["time"] > pd.Timestamp("2016-10-07 00:00:00"))]
                del temp_df["time"]
                return temp_df

            def multi_sample(data_df, offset):
                temp_df = data_df.copy()
                hour_offset = offset / 3
                minute_offset = (offset % 3) * 20
                # 增加filter，只要下午的数据
                # temp_df = temp_df[temp_df["hour"] >= 14]
                append_data = temp_df[(temp_df["hour"] == 17 + hour_offset) & (temp_df["minute"] == minute_offset)]
                # print 'before appending : ' + str(temp_df.shape)
                for i in range(10):
                    temp_df = temp_df.append(append_data, ignore_index=True)
                # print "after appending : " + str(temp_df.shape)
                return temp_df

            def multi_sample_morning(data_df, offset):
                temp_df = data_df.copy()
                hour_offset = offset / 3
                minute_offset = (offset % 3) * 20
                # 增加filter，只要上午的数据
                # temp_df = temp_df[temp_df["hour"] < 14]
                append_data = temp_df[(temp_df["hour"] == 8 + hour_offset) & (temp_df["minute"] == minute_offset)]
                # print 'before appending : ' + str(temp_df.shape)
                for i in range(10):
                    temp_df = temp_df.append(append_data, ignore_index=True)
                # print "after appending : " + str(temp_df.shape)
                return temp_df
                # return data_df.copy()

            # models_entry = []
            train_entry_lst = []
            train_entry_len = 0
            train_entry_score = 0
            for j in range(6):
                train_df = generate_features(volume_entry, new_index_entry, j, file_path=None,
                                             has_type=False)
                # train_df = filter_error3(train_df.fillna(0))
                # print "shape before transformation: " + str(train_df.shape[0])
                # train_df = filter_error2(train_df.fillna(0))
                # train_df = filter_error(train_df.fillna(0), entry_mean, entry_std, entry_max, entry_min)
                # print "shape after transformation: " + str(train_df.shape[0])
                # train_df = train_df[train_df["y"] >= 0]

                # 生成数据时可以注释掉下面涉及该收费站该方向的所有代码
                # 模型分上下午
                # train_X = multi_sample_morning(train_df, j)
                # 保存数据
                # if entry_file_path:
                #     train_X.to_csv(entry_file_path + "_offset_" + str(j) + "_morning.csv")
                # train_y = np.log(1 + train_X["y"].fillna(0)).copy()
                # del train_X["y"]
                # train_entry_len += len(train_y)
                # estimator1 = gbdt_model(train_X, train_y)
                # train2_X = multi_sample(train_df, j)
                # if entry_file_path:
                #     train2_X.to_csv(entry_file_path + "_offset_" + str(j) + "_afternoon.csv")
                # train2_y = np.log1p(train2_X["y"].fillna(0)).copy()
                # del train2_X["y"]
                # estimator2 = gbdt_model(train2_X, train2_y)
                # models_entry.append([estimator1, estimator2])
                train_entry_lst.append(train_df.fillna(0))

            # 注意！！！！2号收费站只有entry方向没有exit方向
            if len(volume_exit) == 0:
                return train_entry_lst, [pd.DataFrame() for i in range(6)]

            old_index_exit = volume_exit.columns
            new_index_exit = []
            for i in range(6):
                new_index_exit += [item + "%d" % (i,) for item in old_index_exit]
            new_index_exit.append("y")
            # models_exit = []
            train_exit_lst = []
            train_exit_len = 0
            train_exit_score = 0
            for j in range(6):
                train_df = generate_features(volume_exit, new_index_exit, j, file_path=exit_file_path, has_type=True)
                # train_df = filter_error3(train_df.fillna(0))
                # print "shape before transformation: " + str(train_df.shape[0])
                # train_df = filter_error2(train_df.fillna(0))
                # train_df = filter_error(train_df.fillna(0), exit_mean, exit_std, exit_max, exit_min)
                # print "shape after transformation: " + str(train_df.shape[0])
                # 生成数据时可以注释掉下面五行
                # 模型分上下午
                # train_X = multi_sample_morning(train_df, j)
                # if exit_file_path:
                #     train_X.to_csv(exit_file_path + "_offset_" + str(j) + "_morning.csv")
                # train_y = np.log(1 + train_X["y"].fillna(0)).copy()
                # del train_X["y"]
                # estimator1 = gbdt_model(train_X, train_y)
                # train2_X = multi_sample(train_df, j)
                # if exit_file_path:
                #     train2_X.to_csv(exit_file_path + "_offset_" + str(j) + "_afternoon.csv")
                # train2_y = np.log1p(train2_X["y"].fillna(0)).copy()
                # del train2_X["y"]
                # estimator2 = gbdt_model(train2_X, train2_y)
                # train_exit_len += len(train_y)
                # train_exit_score += scorer2(best_estimator, train_X, train_y)
                # models_exit.append([estimator1, estimator2])
                train_exit_lst.append(train_df.fillna(0))
            # print "Best Score is :", train_exit_score / train_exit_len

            # return models_entry, models_exit
            return train_entry_lst, train_exit_lst

        # 创建车流量预测集，20分钟跨度有关系的预测集
        def divide_test_by_direction(volume_df, entry_file_path=None, exit_file_path=None, has_type=False):
            global entry_mean
            global entry_std
            global entry_max
            global entry_min
            global exit_mean
            global exit_std
            global exit_max
            global exit_min
            volume_entry_test = volume_df[
                (volume_df['tollgate_id'] == tollgate_id) & (volume_df["direction"] == "entry")].copy()
            volume_entry_test["volume"] = 1

            volume_entry_test["etc_count"] = volume_entry_test["has_etc"].apply(lambda x: 1 if x == "Yes" else 0)
            volume_entry_test["etc_model"] = volume_entry_test["etc_count"] * volume_entry_test["vehicle_model"]
            volume_entry_test["no_etc_count"] = volume_entry_test["has_etc"].apply(lambda x: 1 if x == "No" else 0)
            volume_entry_test["no_etc_model"] = volume_entry_test["no_etc_count"] * volume_entry_test["vehicle_model"]
            # 这里entry方向数据不记录车辆类型，所以特征稍微少一点
            volume_entry_test["model02_count"] = volume_entry_test["vehicle_model"].apply(
                lambda x: 1 if x >= 0 and x <= 2 else 0)
            volume_entry_test["model02_model"] = volume_entry_test["vehicle_model"] * volume_entry_test["model02_count"]
            volume_entry_test["model35_count"] = volume_entry_test["vehicle_model"].apply(
                lambda x: 1 if x >= 3 and x <= 5 else 0)
            volume_entry_test["model35_model"] = volume_entry_test["vehicle_model"] * volume_entry_test["model35_count"]
            volume_entry_test["model67_count"] = volume_entry_test["vehicle_model"].apply(
                lambda x: 1 if x == 6 or x == 7 else 0)
            volume_entry_test["model67_model"] = volume_entry_test["vehicle_model"] * volume_entry_test["model67_count"]
            volume_entry_test.index = volume_entry_test["time"]
            del volume_entry_test["time"]
            del volume_entry_test["tollgate_id"]
            del volume_entry_test["direction"]
            del volume_entry_test["vehicle_type"]
            del volume_entry_test["has_etc"]
            volume_entry_test = volume_entry_test.resample("20T").sum()
            volume_entry_test = volume_entry_test.dropna()
            # entry_mean = volume_entry_test["volume"].mean()
            # entry_std = volume_entry_test["volume"].std()
            # entry_max = volume_entry_test["volume"].max()
            # entry_min = volume_entry_test["volume"].min()

            volume_exit_test = volume_df[
                (volume_df['tollgate_id'] == tollgate_id) & (volume_df["direction"] == "exit")].copy()
            if len(volume_exit_test) > 0:
                volume_exit_test["volume"] = 1
                volume_exit_test["etc_count"] = volume_exit_test["has_etc"].apply(lambda x: 1 if x == "Yes" else 0)
                volume_exit_test["etc_model"] = volume_exit_test["etc_count"] * volume_exit_test["vehicle_model"]
                volume_exit_test["no_etc_count"] = volume_exit_test["has_etc"].apply(lambda x: 1 if x == "No" else 0)
                volume_exit_test["no_etc_model"] = volume_exit_test["no_etc_count"] * volume_exit_test[
                    "vehicle_model"]
                volume_exit_test["cargo_count"] = volume_exit_test["vehicle_type"].apply(
                    lambda x: 1 if x == "cargo" else 0)
                volume_exit_test["passenger_count"] = volume_exit_test["vehicle_type"].apply(
                    lambda x: 1 if x == "passenger" else 0)
                # volume_exit_test["no_count"] = volume_exit_test["vehicle_type"].apply(lambda x: 1 if x == "No" else 0)
                volume_exit_test["cargo_model"] = volume_exit_test["cargo_count"] * volume_exit_test["vehicle_model"]
                volume_exit_test["passenger_model"] = volume_exit_test["passenger_count"] * volume_exit_test[
                    "vehicle_model"]
                # volume_exit_test["model02_count"] = volume_exit_test["vehicle_model"].apply(
                #     lambda x: 1 if x >= 0 and x <= 2 else 0)
                # volume_exit_test["model35_count"] = volume_exit_test["vehicle_model"].apply(
                #     lambda x: 1 if x >= 3 and x <= 5 else 0)
                # volume_exit_test["model67_count"] = volume_exit_test["vehicle_model"].apply(
                #     lambda x: 1 if x == 6 or x == 7 else 0)
                volume_exit_test.index = volume_exit_test["time"]
                del volume_exit_test["time"]
                del volume_exit_test["tollgate_id"]
                del volume_exit_test["direction"]
                del volume_exit_test["vehicle_type"]
                del volume_exit_test["has_etc"]
                volume_exit_test = volume_exit_test.resample("20T").sum()
                volume_exit_test = volume_exit_test.dropna()
                volume_exit_test["cargo_model_avg"] = volume_exit_test["cargo_model"] / volume_exit_test["cargo_count"]
                volume_exit_test["passenger_model_avg"] = volume_exit_test["passenger_model"] / volume_exit_test[
                    "passenger_count"]
                volume_exit_test["vehicle_model_avg"] = volume_exit_test["vehicle_model"] / volume_exit_test["volume"]
                volume_exit_test = volume_exit_test.fillna(0)
                # exit_mean = volume_exit_test["volume"].mean()
                # exit_std = volume_exit_test["volume"].std()
                # exit_max = volume_exit_test["volume"].max()
                # exit_min = volume_exit_test["volume"].min()
            if entry_file_path:
                volume_entry_test.to_csv(entry_file_path, encoding="utf8")
            if exit_file_path:
                volume_exit_test.to_csv(exit_file_path, encoding="utf8")
            return volume_entry_test, volume_exit_test

        # 转换预测集，将预测集转换成与训练集格式相同的格式
        def predict(volume_entry_test, volume_exit_test, models_entry, models_exit,
                    entry_file_path=None, exit_file_path=None):
            old_index_entry = volume_entry_test.columns
            new_index_entry = []
            for i in range(6):
                new_index_entry += [item + "%d" % (i,) for item in old_index_entry]

            # （entry方向）
            test_entry_df = pd.DataFrame()
            i = 0
            while i < len(volume_entry_test) - 5:
                se_temp = pd.Series()
                for k in range(6):
                    se_temp = se_temp.append(volume_entry_test.iloc[i + k, :])
                se_temp.index = new_index_entry
                se_temp.name = str(volume_entry_test.index[i])
                test_entry_df = test_entry_df.append(se_temp)
                i += 6
            test_entry_df = generate_2hours_features(test_entry_df, 0, has_type=False)
            predict_test_entry = pd.DataFrame()
            new_index = None
            test_entry_lst = []
            for i in range(6):
                # if entry_file_path:
                #     test_entry_df = generate_time_features(test_entry_df, i + 6, entry_file_path + "offset" + str(i))
                # else:
                #     test_entry_df = generate_time_features(test_entry_df, i + 6)
                test_entry_df = generate_time_features(test_entry_df, i + 6)
                # 生成数据时可以注释掉三行
                # test_entry_df1 = test_entry_df[test_entry_df["hour"] < 12]
                # test_entry_df2 = test_entry_df[test_entry_df["hour"] > 12]
                # if entry_file_path:
                #     test_entry_df1.to_csv(entry_file_path + "_offset_" + str(i) + "_morning.csv")
                #     test_entry_df2.to_csv(entry_file_path + "_offset_" + str(i) + "_afternoon.csv")
                # test1_y = models_entry[i][0].predict(test_entry_df1)
                # test2_y = models_entry[i][1].predict(test_entry_df2)
                # test_y = np.append(test1_y, test2_y)
                # test_y = models_entry[i].predict(test_entry_df)
                # predict_test_entry[i] = np.exp(test_y) - 1
                # new_index = np.append(test_entry_df1.index.values, test_entry_df2.index.values)
                test_entry_lst.append(test_entry_df)
            # predict_test_entry.index = new_index

            # （exit方向）
            test_exit_df = pd.DataFrame()
            if tollgate_id == "2S":
                return test_entry_lst, [pd.DataFrame() for i in range(6)]

            old_index_exit = volume_exit_test.columns
            new_index_exit = []
            for i in range(6):
                new_index_exit += [item + "%d" % (i,) for item in old_index_exit]
            i = 0
            while i < len(volume_exit_test) - 5:
                se_temp = pd.Series()
                for k in range(6):
                    se_temp = se_temp.append(volume_exit_test.iloc[i + k, :])
                se_temp.index = new_index_exit
                se_temp.name = str(volume_exit_test.index[i])
                test_exit_df = test_exit_df.append(se_temp)
                i += 6
            test_exit_df = generate_2hours_features(test_exit_df, 0, has_type=True)
            predict_test_exit = pd.DataFrame()
            test_exit_lst = []
            for i in range(6):
                # if exit_file_path:
                #     test_exit_df = generate_time_features(test_exit_df, i + 6, exit_file_path + "offset" + str(i))
                # else:
                #     test_exit_df = generate_time_features(test_exit_df, i + 6)
                test_exit_df = generate_time_features(test_exit_df, i + 6)
                # 生成数据时可以注释掉三行
                # test_exit_df1 = test_exit_df[test_exit_df["hour"] < 12]
                # test_exit_df2 = test_exit_df[test_exit_df["hour"] > 12]
                # if exit_file_path:
                #     test_exit_df1.to_csv(exit_file_path + "_offset_" + str(i) + "_morning.csv")
                #     test_exit_df2.to_csv(exit_file_path + "_offset_" + str(i) + "_afternoon.csv")
                # test1_y = models_exit[i][0].predict(test_exit_df1)
                # test2_y = models_exit[i][1].predict(test_exit_df2)
                # test_y = np.append(test1_y, test2_y)
                # test_y = models_exit[i].predict(test_exit_df)
                # predict_test_exit[i] = np.exp(test_y) - 1
                # new_index = np.append(test_exit_df1.index.values, test_exit_df2.index.values)
                test_exit_lst.append(test_exit_df)
            # predict_test_exit.index = test_exit_df.index
            # predict_test_exit.index = new_index
            return test_entry_lst, test_exit_lst

        # 将预测数据转换成输出文件的格式
        def transform_predict(predict_original, direction, tollgate_id):
            result = pd.DataFrame()
            for i in range(len(predict_original)):
                time_basic = predict_original.index[i]
                for j in range(6, 12, 1):
                    time_window = "[" + str(pd.Timestamp(time_basic) + DateOffset(minutes=j * 20)) + "," + str(
                        pd.Timestamp(time_basic) + DateOffset(minutes=(j + 1) * 20)) + ")"
                    series = pd.Series({"tollgate_id": tollgate_id,
                                        "time_window": time_window,
                                        "direction": direction,
                                        "volume": "%.2f" % (predict_original.iloc[i, j - 6])})
                    series.name = i + j - 6
                    result = result.append(series)
            return result

        def add_history(data_df):
            data = data_df.copy()
            data["time"] = data.index
            data["time"] = data["time"].apply(lambda x: pd.Timestamp(x))
            for i in range(4):
                data_temp = data[["volume5", "time"]].copy()
                data_temp.rename(columns={"volume5": "y"})
                data_temp["time"] = data_temp["time"] + DateOffset(days=i * 2 + 1)
                data = pd.merge(data, data_temp, how="left", on=["time"], suffixes=["", "_" + str(i)])
            # data_temp = data_df[["y", "time"]].copy()
            # data_temp["time"] = data_temp["time"] + DateOffset(days=i + 7)
            # data = pd.merge(data, data_temp, how="left", on=["time"], suffixes=["", "_7"])
            del data["time"]
            return data

        def filter_error(data_df):
            temp_df = data_df.copy()
            # temp_df["time"] = temp_df.index
            # temp_df["time"] = temp_df["time"].apply(pd.Timestamp)
            # temp_df = temp_df[(temp_df["time"] < pd.Timestamp("2016-09-30 22:20:00")) |
            #                   (temp_df["time"] > pd.Timestamp("2016-10-07 00:00:00"))]
            # del temp_df["time"]
            return temp_df.dropna()

        def multi_sample_afternoon(data_df, offset):
            temp_df = data_df.copy()
            hour_offset = offset / 3
            minute_offset = (offset % 3) * 20
            # 增加filter，只要下午的数据
            append_data = temp_df[(temp_df["hour"] == 17 + hour_offset) & (temp_df["minute"] == minute_offset)]
            for i in range(10):
                temp_df = temp_df.append(append_data, ignore_index=True)
            return temp_df

        def multi_sample_morning(data_df, offset):
            temp_df = data_df.copy()
            hour_offset = offset / 3
            minute_offset = (offset % 3) * 20
            # 增加filter，只要上午的数据
            append_data = temp_df[(temp_df["hour"] == 8 + hour_offset) & (temp_df["minute"] == minute_offset)]
            for i in range(10):
                temp_df = temp_df.append(append_data, ignore_index=True)
            return temp_df

        def split_data(data_df):
            del data_df["y"]
            data1 = data_df[data_df["hour"] < 12]
            data2 = data_df[data_df["hour"] > 12]
            return [data1, data2]

        entry_train_file = "./train&test1_zjw/volume2_entry_train_%s" % (tollgate_id,)
        exit_train_file = "./train&test1_zjw/volume2_exit_train_%s" % (tollgate_id,)
        entry_test_file = "./train&test1_zjw/volume2_entry_test_%s" % (tollgate_id,)
        exit_test_file = "./train&test1_zjw/volume2_exit_test_%s" % (tollgate_id,)

        entry_test, exit_test = divide_test_by_direction(volume_test)
        volume_entry_train, volume_exit_train = divide_train_by_direction(volume_train)
        train_entry, train_exit = generate_models(volume_entry_train,
                                                    volume_exit_train)
        test_entry, test_exit = predict(entry_test,
                                        exit_test,
                                        [],
                                        [])
        n_entry_trains = [train_entry[i].shape[0] for i in range(6)]
        all_data_entry = [add_history(pd.concat((train_entry[i], test_entry[i]))) for i in range(6)]
        train_entry = [[multi_sample_morning(filter_error(all_data_entry[i][:n_entry_trains[i]]), i),
                        multi_sample_afternoon(filter_error(all_data_entry[i][:n_entry_trains[i]]), i)]
                       for i in range(len(all_data_entry))]
        test_entry = [split_data(all_data_entry[i][n_entry_trains[i]:])
                      for i in range(len(all_data_entry))]
        # 输出文件
        for i in range(len(train_entry)):
            train = train_entry[i]
            train[0].to_csv("./train&test1_zjw/volume_entry_train_" + tollgate_id + "_offset_" + str(i) + "_morning.csv")
            train[1].to_csv("./train&test1_zjw/volume_entry_train_" + tollgate_id + "_offset_" + str(i) + "_afternoon.csv")
        for i in range(len(test_entry)):
            test = test_entry[i]
            test[0].to_csv("./train&test1_zjw/volume_entry_test_" + tollgate_id + "_offset_" + str(i) + "_morning.csv")
            test[1].to_csv("./train&test1_zjw/volume_entry_test_" + tollgate_id + "_offset_" + str(i) + "_afternoon.csv")

        n_exit_trains = [train_exit[0].shape[0] for i in range(6)]
        if n_exit_trains[0] > 0:
            all_data_exit = [add_history(pd.concat((train_exit[i], test_exit[i]))) for i in range(6)]
            train_exit = [[multi_sample_morning(filter_error(all_data_exit[i][:n_exit_trains[i]]), i),
                           multi_sample_afternoon(filter_error(all_data_exit[i][:n_exit_trains[i]]), i)]
                          for i in range(len(all_data_exit))]
            test_exit = [split_data(all_data_exit[i][n_exit_trains[i]:])
                         for i in range(len(all_data_exit))]
            # 输出文件
            for i in range(len(train_exit)):
                train = train_entry[i]
                train[0].to_csv("./train&test1_zjw/volume_exit_train_" + tollgate_id + "_offset_" + str(i) + "_morning.csv")
                train[1].to_csv("./train&test1_zjw/volume_exit_train_" + tollgate_id + "_offset_" + str(i) + "_afternoon.csv")
            for i in range(len(test_exit)):
                test = test_entry[i]
                test[0].to_csv("./train&test1_zjw/volume_exit_test_" + tollgate_id + "_offset_" + str(i) + "_morning.csv")
                test[1].to_csv("./train&test1_zjw/volume_exit_test_" + tollgate_id + "_offset_" + str(i) + "_afternoon.csv")

        # result_df = result_df.append(transform_predict(predict_original_entry, "entry", tollgate_id))
        # result_df = result_df.append(transform_predict(predict_original_exit, "exit", tollgate_id))

    return result_df


result = modeling()
# result_df = pd.DataFrame()
# result_df["tollgate_id"] = result["tollgate_id"].replace({"1S": 1, "2S": 2, "3S": 3})
# result_df["time_window"] = result["time_window"]
# result_df["direction"] = result["direction"].replace({"entry": 0, "exit": 1})
# result_df['volume'] = result["volume"]
# result_df.to_csv("volume_predict_multi_sample10.csv", encoding="utf8", index=None)
