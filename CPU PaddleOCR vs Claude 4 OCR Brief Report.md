# CPU PaddleOCR vs Claude 4 OCR Brief Report

## 1. Data & Directory Structure

\# Data & Directory Structure

The dataset is organized by model type. Each subfolder contains raw images, PaddleOCR JSON/Markdown outputs, and Claude 4 Markdown/JSON outputs.
```
comparison/
├── larger_model_over_150s/         # large model (>150s CPU inference)
│   ├── handwritten_nums/           # handwritten text
│   ├── low_quality_nums/           # blurred/compressed text
│   ├── noisy_handwritten_nums/     # noisy handwritten text
│   └── watermark/                  # images with watermark/logo
│
└── light_model_within_20s/         # lightweight model (<20s CPU inference)
    ├── handwritten_nums/
    ├── low_quality_nums/
    ├── noisy_handwritten_nums/
    └── watermark/
```

\# Each subfolder contains:

\# - raw images

\# - PaddleOCR output (.md or .json)

\# - Claude 4 output (.md or .json)


## 2. PaddleOCR Summary
- Fast (light model), much slow but slightly better (large model)  
- No semantic correction  
- Frequent spelling errors in English  


## 3. Claude 4 Summary
- Performance on weak visual input is better than paddleocr
- Semantic reconstruction of unclear text  
- Can produce more accurate structured Markdown/table  


## 4. Key Findings
1. Claude 4 greatly outperforms on handwritten & blurred digits  
2. PaddleOCR lacks semantic reasoning  


## 5. Next Steps
- Try GPU acceleration for PaddleOCR with larger model
- Test real watermark images to reproduce token inflation  


## 6. Conclusion
Claude 4 delivers higher OCR quality in complex settings,  
while PaddleOCR is limited by visual architecture and CPU.  
Semantic reconstruction becomes a major advantage.