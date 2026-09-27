"""Tải dữ liệu khớp lệnh (aggTrades) từ data.binance.vision và tóm gọn thành từng phút.

Mỗi phút có: giá mở/cao/thấp/đóng, khối lượng mua chủ động, bán chủ động, số lệnh,
và khối lượng của các lệnh lớn (lệnh cá mập).
Chạy: python aggflow.py BTCUSDT 12   (12 tháng gần nhất)
"""
import sys, os, io, zipfile, datetime as dt, urllib.request
import numpy as np, pandas as pd

SYM = sys.argv[1] if len(sys.argv) > 1 else 'BTCUSDT'
MONTHS = int(sys.argv[2]) if len(sys.argv) > 2 else 12
BIG = {'BTCUSDT': 1.0, 'ETHUSDT': 20.0, 'SOLUSDT': 500.0, 'BNBUSDT': 50.0}.get(SYM, 0)  # ngưỡng lệnh lớn (đơn vị coin)
OUT = f'data/aggflow/{SYM}'
os.makedirs(OUT, exist_ok=True)
COLS = ['id', 'price', 'qty', 'first', 'last', 'ts', 'buyer_maker', 'best']


def months_back(n):
    d = dt.date.today().replace(day=1)
    for _ in range(n):
        d = (d - dt.timedelta(days=1)).replace(day=1)
        yield d.strftime('%Y-%m')


def process(ym):
    path = f'{OUT}/{SYM}-1m-{ym}.csv.gz'
    if os.path.exists(path):
        print('da co', path); return
    url = f'https://data.binance.vision/data/spot/monthly/aggTrades/{SYM}/{SYM}-aggTrades-{ym}.zip'
    tmp = f'/tmp/{SYM}-{ym}.zip'
    print('tai', url, flush=True)
    try:
        urllib.request.urlretrieve(url, tmp)
    except Exception as e:
        print('  khong tai duoc:', e); return
    parts = []
    with zipfile.ZipFile(tmp) as z:
        with z.open(z.namelist()[0]) as f:
            for ch in pd.read_csv(f, header=None, names=COLS, usecols=['price', 'qty', 'ts', 'buyer_maker'],
                                  chunksize=5_000_000, dtype={'price': 'float64', 'qty': 'float64'}):
                if isinstance(ch.ts.iloc[0], str): ch = ch[ch.ts != 'transact_time']  # bỏ dòng tiêu đề nếu có
                ts = ch.ts.astype('int64')
                unit = 'us' if ts.iloc[0] > 1e14 else 'ms'
                ch = ch.assign(t=pd.to_datetime(ts, unit=unit).dt.floor('1min'))
                bm = ch.buyer_maker.astype(str).str.lower() == 'true'   # True = bên bán chủ động
                ch = ch.assign(buy=np.where(~bm, ch.qty, 0.0), sell=np.where(bm, ch.qty, 0.0),
                               bigbuy=np.where(~bm & (ch.qty >= BIG), ch.qty, 0.0),
                               bigsell=np.where(bm & (ch.qty >= BIG), ch.qty, 0.0))
                g = ch.groupby('t').agg(open=('price', 'first'), high=('price', 'max'), low=('price', 'min'),
                                        close=('price', 'last'), buy=('buy', 'sum'), sell=('sell', 'sum'),
                                        n=('qty', 'size'), bigbuy=('bigbuy', 'sum'), bigsell=('bigsell', 'sum'))
                parts.append(g)
    os.remove(tmp)
    m = pd.concat(parts)
    m = m.groupby(level=0).agg(open=('open', 'first'), high=('high', 'max'), low=('low', 'min'), close=('close', 'last'),
                               buy=('buy', 'sum'), sell=('sell', 'sum'), n=('n', 'sum'),
                               bigbuy=('bigbuy', 'sum'), bigsell=('bigsell', 'sum'))
    m.round(6).to_csv(path, compression='gzip')
    print('  xong', path, len(m), 'phut', flush=True)


for ym in months_back(MONTHS):
    process(ym)
