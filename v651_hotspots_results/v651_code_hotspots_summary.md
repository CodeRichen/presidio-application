# V6.5.1：`handler_code.detect()` 內部熱點分析

- 狀態：**PROFILE_OK**
- 固定七頁合成 PDF、原本 2×+3× OCR、原本 V5 PDF 輸出。
- 不修改正式專案檔案；只在子程序中包裝函式並抽樣。
- cProfile 有測量開銷，因此執行時間不可直接視為正式版速度。
- 不記錄 OCR 字串、函式參數、私有 map、圖片或雜湊。

- `handler_code.detect` 總呼叫：278；其中 cProfile 抽樣：70
- 本次完整處理時間（含分析開銷）：218.241 秒
- 27/27：27/27；17/17：17/17；PDF 安全：True

## 函式自身耗時（self time）前 20 名

| 檔案 | 函式 | 總呼叫 | 自身秒數 | 累積秒數 |
|---|---|---:|---:|---:|
| `<builtin>` | `<built-in_method_torch._C._nn.linear>` | 10220 | 5.05584 | 5.05584 |
| `thread.py` | `submit` | 199 | 0.82732 | 0.83459 |
| `core_model_loading.py` | `_materialize_copy` | 199 | 0.37863 | 0.0014 |
| `<builtin>` | `<built-in_method_torch.layer_norm>` | 3500 | 0.33083 | 0.33083 |
| `<builtin>` | `<built-in_method_torch._C._nn.gelu>` | 1680 | 0.32454 | 0.32454 |
| `<builtin>` | `<built-in_method_torch._C._nn.scaled_dot_product_attention>` | 1680 | 0.24988 | 0.24988 |
| `module.py` | `_call_impl` | 30100 | 0.15837 | 7.53297 |
| `modeling_bert.py` | `forward` | 1680 | 0.10214 | 2.11396 |
| `module.py` | `_wrapped_call_impl` | 30100 | 0.09722 | 7.53397 |
| `module.py` | `__getattr__` | 62385 | 0.09012 | 0.09012 |
| `<builtin>` | `<method__to__of__torch._C.TensorBase__objects>` | 1879 | 0.07831 | 0.63482 |
| `tokenization_bert.py` | `__init__` | 1 | 0.07514 | 0.08075 |
| `token_classification.py` | `gather_pre_entities` | 140 | 0.0743 | 0.13877 |
| `modeling_bert.py` | `forward` | 1680 | 0.07352 | 2.25616 |
| `<builtin>` | `<method__transpose__of__torch._C.TensorBase__objects>` | 6720 | 0.07336 | 0.07336 |
| `<builtin>` | `<built-in_method_torch._C._get_tracing_state>` | 31892 | 0.07152 | 0.07152 |
| `modeling_bert.py` | `forward` | 1680 | 0.06731 | 0.79721 |
| `<builtin>` | `<method__view__of__torch._C.TensorBase__objects>` | 5040 | 0.05482 | 0.05482 |
| `core_model_loading.py` | `_job` | 199 | 0.05172 | 0.68659 |
| `inspect.py` | `_signature_from_callable` | 3363 | 0.04905 | 0.19733 |

## 累積耗時（cumulative time）前 20 名

| 檔案 | 函式 | 總呼叫 | 自身秒數 | 累積秒數 |
|---|---|---:|---:|---:|
| `handler_code.py` | `detect` | 70 | 0.00077 | 9.81421 |
| `handler_text.py` | `_pii_zh_sp_shared` | 70 | 0.00046 | 9.80618 |
| `handler_text.py` | `zh_ner` | 70 | 0.00151 | 9.80565 |
| `token_classification.py` | `__call__` | 140 | 0.00235 | 7.99109 |
| `base.py` | `__call__` | 140 | 0.00806 | 7.98794 |
| `<builtin>` | `<built-in_method_builtins.next>` | 1853 | 0.00444 | 7.94804 |
| `pt_utils.py` | `__next__` | 140 | 0.00232 | 7.94529 |
| `pt_utils.py` | `__next__` | 140 | 0.00107 | 7.70153 |
| `base.py` | `forward` | 140 | 0.00545 | 7.57431 |
| `token_classification.py` | `_forward` | 140 | 0.00174 | 7.5368 |
| `module.py` | `_wrapped_call_impl` | 30100 | 0.09722 | 7.53397 |
| `module.py` | `_call_impl` | 30100 | 0.15837 | 7.53297 |
| `generic.py` | `wrapper` | 140 | 0.00175 | 7.53157 |
| `modeling_bert.py` | `forward` | 140 | 0.00343 | 7.52795 |
| `generic.py` | `wrapper` | 140 | 0.00341 | 7.48403 |
| `output_capturing.py` | `wrapper` | 140 | 0.00459 | 7.47361 |
| `modeling_bert.py` | `forward` | 140 | 0.00342 | 7.45848 |
| `modeling_bert.py` | `forward` | 140 | 0.01849 | 7.32132 |
| `modeling_layers.py` | `__call__` | 1680 | 0.01102 | 7.28083 |
| `modeling_bert.py` | `forward` | 1680 | 0.01643 | 7.24365 |

## 解讀限制

此處的累積時間是重疊的，不可加總；僅取樣部分呼叫，也未驗證任何新加速方案。
只有確定真正的慢函式後才考慮局部修改，後續需重新進行像素等價與安全回歸。
GT 及人臉框覆蓋是固定合成案例，不代表真實文件偵測率。
