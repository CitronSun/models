---
license: apache-2.0
library_name: PaddleOCR
language:
- en
- zh
pipeline_tag: image-to-text
tags:
- OCR
- PaddlePaddle
- PaddleOCR
- layout_detection
---

# PP-DocBlockLayout

## Introduction

A layout block localization model trained on a self-built dataset containing Chinese and English papers, PPT, multi-layout magazines, contracts, books, exams, ancient books and research reports using RT-DETR-L. The layout detection model includes 1 category: Region.

| Model| mAP(0.5) (%) | 
|  --- | --- | 
|PP-DocBlockLayout |  95.9 | 

**Note**: the evaluation set of the above precision indicators is the self built version sub area detection data set, including Chinese and English papers, magazines, newspapers, research reports PPT、 1000 document type pictures such as test papers and textbooks.

## Quick Start

### Installation

1. PaddlePaddle

Please refer to the following commands to install PaddlePaddle using pip:

```bash
# for CUDA11.8
python -m pip install paddlepaddle-gpu==3.0.0 -i https://www.paddlepaddle.org.cn/packages/stable/cu118/

# for CUDA12.6
python -m pip install paddlepaddle-gpu==3.0.0 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/

# for CPU
python -m pip install paddlepaddle==3.0.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
```

For details about PaddlePaddle installation, please refer to the [PaddlePaddle official website](https://www.paddlepaddle.org.cn/en/install/quick).

2. PaddleOCR

Install the latest version of the PaddleOCR inference package from PyPI:

```bash
python -m pip install paddleocr
```

### Model Usage

You can quickly experience the functionality with a single command:

```bash
paddleocr layout_detection --model_name PP-DocBlockLayout -i https://cdn-uploads.huggingface.co/production/uploads/63d7b8ee07cd1aa3c49a2026/SCL4KLVcaUKkinua_bTec.png
```

You can also integrate the model inference of the LayoutDetection module into your project. Before running the following code, please download the sample image to your local machine.

```python
from paddleocr import LayoutDetection

model = LayoutDetection(model_name="PP-DocBlockLayout")
output = model.predict("SCL4KLVcaUKkinua_bTec.png", batch_size=1, layout_nms=True)
for res in output:
    res.print()
    res.save_to_img(save_path="./output/")
    res.save_to_json(save_path="./output/res.json")
```

After running, the obtained result is as follows:

```json
{'res': {'input_path': '/root/.paddlex/predict_input/SCL4KLVcaUKkinua_bTec.png', 'page_index': None, 'boxes': [{'cls_id': 0, 'label': 'Region', 'score': 0.9768685698509216, 'coordinate': [31.313992, 298.04843, 479.92798, 1994.14]}, {'cls_id': 0, 'label': 'Region', 'score': 0.9728955626487732, 'coordinate': [648.478, 1233.5554, 1552.8765, 1992.712]}, {'cls_id': 0, 'label': 'Region', 'score': 0.9725626707077026, 'coordinate': [647.51337, 295.63956, 1550.7095, 1181.5878]}, {'cls_id': 0, 'label': 'Region', 'score': 0.9079533219337463, 'coordinate': [644.75916, 59.31064, 1468.8861, 264.68124]}, {'cls_id': 0, 'label': 'Region', 'score': 0.8413463234901428, 'coordinate': [31.890125, 60.103912, 470.73123, 284.72952]}]}}
```

The visualized image is as follows:

![image/jpeg](https://cdn-uploads.huggingface.co/production/uploads/63d7b8ee07cd1aa3c49a2026/Oh3-zU4R3wnkmvX-cY4Tz.png)

For details about usage command and descriptions of parameters, please refer to the [Document](https://paddlepaddle.github.io/PaddleOCR/latest/en/version3.x/module_usage/layout_detection.html#iii-quick-integration).


## Links

[PaddleOCR Repo](https://github.com/paddlepaddle/paddleocr)

[PaddleOCR Documentation](https://paddlepaddle.github.io/PaddleOCR/latest/en/index.html)

