import os

# numpy/scipy's OpenBLAS otherwise spawns a thread per CPU and reserves ~1.5 GB; inherited by the audio engine
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

from unified import main

if __name__ == '__main__':
    main()
