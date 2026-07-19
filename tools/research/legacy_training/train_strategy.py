#!/usr/bin/env python3
"""
量化策略训练脚本 - 10轮训练验证
"""
import requests
import time
import json
from datetime import datetime, timedelta
from pathlib import Path

def get_kline_extended(ts_code, target_beg, target_end):
    """获取目标期间及之前的K线数据"""
    if ts_code.startswith('6'):
        secid = '1.' + ts_code
    else:
        secid = '0.' + ts_code
    
    beg_dt = datetime.strptime(target_beg, '%Y%m%d') - timedelta(days=60)
    beg = beg_dt.strftime('%Y%m%d')
    
    url = f'http://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=0&beg={beg}&end={target_end}'
    try:
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10).json()
        if res.get('data') and res['data'].get('klines'):
            return res['data']['klines']
    except:
        pass
    return []

def analyze_month(klines, target_beg, target_end):
    """分析单月数据"""
    results = []
    
    for i in range(2, len(klines)):
        try:
            day_before = klines[i-2].split(',')
            yesterday = klines[i-1].split(',')
            today = klines[i].split(',')
            
            db_close = float(day_before[2])
            yes_close = float(yesterday[2])
            yes_open = float(yesterday[1])
            today_close = float(today[2])
            
            today_date = today[0]
            if today_date > target_end:
                break
            
            if db_close <= 0 or yes_close <= 0:
                continue
            
            yes_pct = (yes_close - db_close) / db_close * 100
            yes_open_pct = (yes_open - db_close) / db_close * 100
            today_pct = (today_close - yes_close) / yes_close * 100
            
            if yes_pct >= 9.5:
                results.append({
                    'open': yes_open_pct,
                    'close': yes_pct,
                    'cont': today_pct >= 9.5
                })
        except:
            continue
    
    return results

def run_round(round_num, train_beg, train_end, val_beg, val_end, codes):
    """执行一轮训练验证"""
    print(f"\n第 {round_num} 轮: 训练{train_beg}~{train_end}, 验证{val_beg}~{val_end}")
    
    # 获取训练数据
    train_all = []
    for code in codes[:150]:
        klines = get_kline_extended(code, train_beg, train_end)
        result = analyze_month(klines, train_beg, train_end)
        if result:
            train_all.extend(result)
        time.sleep(0.03)
    
    print(f"  训练样本: {len(train_all)}个涨停")
    
    # 获取验证数据
    val_all = []
    for code in codes[:150]:
        klines = get_kline_extended(code, val_beg, val_end)
        result = analyze_month(klines, val_beg, val_end)
        if result:
            val_all.extend(result)
        time.sleep(0.03)
    
    print(f"  验证样本: {len(val_all)}个涨停")
    
    # 分析
    if not train_all or not val_all:
        return None
    
    best_conditions = []
    
    for min_open in range(-5, 12, 1):
        max_open = min_open + 2
        if max_open > 15:
            break
        
        train_subset = [t for t in train_all if min_open <= t['open'] < max_open]
        if len(train_subset) < 2:
            continue
        
        train_cont = sum(1 for t in train_subset if t['cont'])
        train_rate = train_cont / len(train_subset)
        
        val_subset = [t for t in val_all if min_open <= t['open'] < max_open]
        if len(val_subset) < 2:
            continue
        
        val_cont = sum(1 for t in val_subset if t['cont'])
        val_rate = val_cont / len(val_subset)
        
        best_conditions.append({
            'range': f'{min_open}%~{max_open}%',
            'train_rate': train_rate,
            'val_rate': val_rate,
            'train_count': len(train_subset),
            'val_count': len(val_subset)
        })
    
    best_conditions.sort(key=lambda x: x['val_rate'], reverse=True)
    
    if best_conditions:
        print(f"  最佳: {best_conditions[0]['range']} 训练{best_conditions[0]['train_rate']*100:.0f}% 验证{best_conditions[0]['val_rate']*100:.0f}%")
    
    return best_conditions[:3]

def main():
    # 生成股票代码
    codes = []
    for i in range(1, 700):
        codes.append(f'{i:06d}')
    for i in range(1, 300):
        codes.append(f'300{i:03d}')
    
    # 10轮训练 (2024年1-10月)
    rounds = [
        (1, "20240101", "20240131", "20240201", "20240229"),
        (2, "20240201", "20240229", "20240301", "20240331"),
        (3, "20240301", "20240331", "20240401", "20240430"),
        (4, "20240401", "20240430", "20240501", "20240531"),
        (5, "20240501", "20240531", "20240601", "20240630"),
        (6, "20240601", "20240630", "20240701", "20240731"),
        (7, "20240701", "20240731", "20240801", "20240831"),
        (8, "20240801", "20240831", "20240901", "20240930"),
        (9, "20240901", "20240930", "20241001", "20241031"),
        (10, "20241001", "20241031", "20241101", "20241130"),
        # 增加2023年
        (11, "20230101", "20230131", "20230201", "20230228"),
        (12, "20230201", "20230228", "20230301", "20230331"),
        (13, "20230301", "20230331", "20230401", "20230430"),
        (14, "20230401", "20230430", "20230501", "20230531"),
        (15, "20230501", "20230531", "20230601", "20230630"),
        (16, "20230601", "20230630", "20230701", "20230731"),
        (17, "20230701", "20230731", "20230801", "20230831"),
        (18, "20230801", "20230831", "20230901", "20230930"),
        (19, "20230901", "20230930", "20231001", "20231031"),
        (20, "20231001", "20231031", "20231101", "20231130"),
        (21, "20231101", "20231130", "20231201", "20231231"),
        
    ]
    
    all_results = []
    
    for round_num, train_beg, train_end, val_beg, val_end in rounds:
        result = run_round(round_num, train_beg, train_end, val_beg, val_end, codes)
        all_results.append({
            'round': round_num,
            'train_period': f'{train_beg}~{train_end}',
            'val_period': f'{val_beg}~{val_end}',
            'best': result
        })
        time.sleep(1)
    
    # 总结
    print("\n" + "="*60)
    print("【10轮训练验证结果】")
    print("="*60)
    
    summary = {}
    for r in all_results:
        if r['best']:
            for b in r['best']:
                key = b['range']
                if key not in summary:
                    summary[key] = []
                summary[key].append(b['val_rate'])
    
    print("\n各条件平均验证准确率:")
    for key, rates in sorted(summary.items(), key=lambda x: sum(x[1])/len(x[1]), reverse=True):
        avg = sum(rates) / len(rates)
        print(f"  {key}: 平均{avg*100:.0f}% ({len(rates)}轮)")
    
    # 保存
    output_path = Path(__file__).with_name("training_results.json")
    with output_path.open('w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {output_path}")

if __name__ == '__main__':
    main()
